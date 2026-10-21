"""Synthetic proxy routing checks; no Google accounts or external services."""

import asyncio
import base64
import hashlib
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import quote, urlsplit

from src.services.browser.cloak_backend import CloakBrowserBackend
from src.services.browser.cloak_proxy import CloakProxyBridge


class CloakProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.servers = []
        self.bridges = []
        self.writers = []

    async def asyncTearDown(self):
        for bridge in self.bridges:
            await bridge.close()
        for writer in self.writers:
            writer.close()
        for server in self.servers:
            server.close()
            await server.wait_closed()

    async def socks_server(self, body, *, accepted=True):
        seen = []

        async def handle(reader, writer):
            self.writers.append(writer)
            version, methods = await reader.readexactly(2)
            self.assertEqual(version, 5)
            self.assertIn(2, await reader.readexactly(methods))
            writer.write(b"\x05\x02")
            await writer.drain()
            self.assertEqual(await reader.readexactly(1), b"\x01")
            username = await reader.readexactly((await reader.readexactly(1))[0])
            password = await reader.readexactly((await reader.readexactly(1))[0])
            seen.append({"username": username, "password": password})
            writer.write(b"\x01\x00" if accepted else b"\x01\x01")
            await writer.drain()
            if not accepted:
                writer.close()
                return
            # Receiving the original .invalid hostname proves DNS is delegated
            # to SOCKS instead of resolving through the host's FakeIP resolver.
            self.assertEqual(await reader.readexactly(4), b"\x05\x01\x00\x03")
            hostname = await reader.readexactly((await reader.readexactly(1))[0])
            port = int.from_bytes(await reader.readexactly(2), "big")
            seen[-1].update(hostname=hostname, port=port)
            writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x01")
            await writer.drain()
            seen[-1]["request"] = await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.servers.append(server)
        port = server.sockets[0].getsockname()[1]
        return port, seen

    async def bridge(self, port, username, password):
        bridge = await CloakProxyBridge.start(
            f"socks5h://{quote(username, safe='')}:{quote(password, safe='')}@127.0.0.1:{port}"
        )
        self.bridges.append(bridge)
        return bridge

    async def connect(self, bridge):
        parsed = urlsplit(bridge.url)
        reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port)
        self.writers.append(writer)
        return reader, writer

    async def test_connect_keeps_remote_dns_and_isolates_account_credentials(self):
        for marker, username, password in (
            (b"account-one", "user@one", "p:a#s/__%"),
            (b"account-two", "user-two", "another-secret"),
        ):
            port, seen = await self.socks_server(marker)
            bridge = await self.bridge(port, username, password)
            reader, writer = await self.connect(bridge)
            writer.write(b"CONNECT login.invalid:443 HTTP/1.1\r\nHost: login.invalid:443\r\n\r\n")
            await writer.drain()
            response = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
            self.assertIn(b"200", response)
            writer.write(b"GET /session HTTP/1.1\r\nHost: login.invalid\r\n\r\n")
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 3)
            self.assertTrue(response.endswith(marker))
            self.assertEqual(seen[0]["hostname"], b"login.invalid")
            self.assertEqual(seen[0]["port"], 443)
            self.assertEqual(seen[0]["username"], username.encode())
            self.assertEqual(seen[0]["password"], password.encode())
            self.assertNotIn(password.encode(), seen[0]["request"])
            self.assertEqual(urlsplit(bridge.url).hostname, "127.0.0.1")
            self.assertIsNone(urlsplit(bridge.url).username)

    async def test_plain_http_routes_through_same_fixed_socks_and_auth_failure_stays_closed(self):
        for accepted in (True, False):
            port, seen = await self.socks_server(b"same-exit", accepted=accepted)
            bridge = await self.bridge(port, "user", "secret")
            reader, writer = await self.connect(bridge)
            writer.write(b"GET http://api.invalid/probe HTTP/1.1\r\nHost: api.invalid\r\n\r\n")
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 3)
            self.assertEqual(response.endswith(b"same-exit"), accepted)
            self.assertEqual(len(seen), 1)
            if accepted:
                self.assertEqual(seen[0]["hostname"], b"api.invalid")
                self.assertEqual(seen[0]["port"], 80)
                self.assertTrue(seen[0]["request"].startswith(b"GET /probe HTTP/1.1"))
            else:
                self.assertNotIn("hostname", seen[0])

    async def test_close_stops_listener_and_active_handshake(self):
        connected = asyncio.Event()

        async def stalled(reader, writer):
            self.writers.append(writer)
            connected.set()
            await reader.read()

        server = await asyncio.start_server(stalled, "127.0.0.1", 0)
        self.servers.append(server)
        bridge = await self.bridge(server.sockets[0].getsockname()[1], "user", "secret")
        reader, writer = await self.connect(bridge)
        writer.write(b"CONNECT login.invalid:443 HTTP/1.1\r\nHost: login.invalid:443\r\n\r\n")
        await writer.drain()
        await asyncio.wait_for(connected.wait(), 3)
        address = urlsplit(bridge.url)
        await asyncio.wait_for(bridge.close(), 3)
        self.assertEqual(await asyncio.wait_for(reader.read(), 3), b"")
        self.assertFalse(bridge._writers)
        self.assertFalse(bridge._handlers)
        with self.assertRaises(OSError):
            await asyncio.open_connection(address.hostname, address.port)

    async def test_authenticated_http_proxy_uses_same_bridge_without_exposing_credentials(self):
        seen = []

        async def handle(reader, writer):
            self.writers.append(writer)
            seen.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            seen.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.servers.append(server)
        port = server.sockets[0].getsockname()[1]
        bridge = await CloakProxyBridge.start(f"http://user:p%3A%23%40@127.0.0.1:{port}")
        self.bridges.append(bridge)
        reader, writer = await self.connect(bridge)
        writer.write(b"CONNECT api.invalid:443 HTTP/1.1\r\nHost: api.invalid:443\r\n\r\n")
        await writer.drain()
        self.assertIn(b"200", await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3))
        writer.write(b"GET / HTTP/1.1\r\nHost: api.invalid\r\n\r\n")
        await writer.drain()
        self.assertTrue((await asyncio.wait_for(reader.read(), 3)).endswith(b"OK"))
        self.assertIn(b"CONNECT api.invalid:443", seen[0])
        self.assertIn(b"Proxy-Authorization: Basic " + base64.b64encode(b"user:p:#@"), seen[0])
        self.assertNotIn(b"Proxy-Authorization", seen[1])

    async def test_host_http_hop_connects_to_fixed_socks_without_changing_remote_dns(self):
        final_port, final_seen = await self.socks_server(b"fixed-egress")
        first_seen = []

        async def tunnel(reader, writer):
            self.writers.append(writer)
            request = await reader.readuntil(b"\r\n\r\n")
            first_seen.append(request)
            upstream_reader, upstream_writer = await asyncio.open_connection(
                "127.0.0.1", final_port
            )
            self.writers.append(upstream_writer)
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()

            async def pipe(source, destination):
                while chunk := await source.read(65536):
                    destination.write(chunk)
                    await destination.drain()
                destination.close()

            await asyncio.gather(pipe(reader, upstream_writer), pipe(upstream_reader, writer))

        server = await asyncio.start_server(tunnel, "127.0.0.1", 0)
        self.servers.append(server)
        first_port = server.sockets[0].getsockname()[1]
        with patch.dict(os.environ, {"LAY4FLOW_HOST_PROXY": f"http://127.0.0.1:{first_port}"}):
            bridge = await self.bridge(final_port, "account-user", "final-secret")
        reader, writer = await self.connect(bridge)
        writer.write(b"CONNECT google.invalid:443 HTTP/1.1\r\nHost: google.invalid:443\r\n\r\n")
        await writer.drain()
        self.assertIn(b"200", await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3))
        writer.write(b"GET /session HTTP/1.1\r\nHost: google.invalid\r\n\r\n")
        await writer.drain()
        self.assertTrue((await asyncio.wait_for(reader.read(), 3)).endswith(b"fixed-egress"))
        self.assertIn(f"CONNECT 127.0.0.1:{final_port}".encode(), first_seen[0])
        self.assertNotIn(b"google.invalid", first_seen[0])
        self.assertNotIn(b"final-secret", first_seen[0])
        self.assertEqual(final_seen[0]["hostname"], b"google.invalid")
        self.assertEqual(final_seen[0]["username"], b"account-user")

    async def test_host_hop_rejection_does_not_fall_back_to_the_fixed_proxy(self):
        final_port, final_seen = await self.socks_server(b"should-not-connect")

        async def reject(reader, writer):
            self.writers.append(writer)
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(reject, "127.0.0.1", 0)
        self.servers.append(server)
        bridge = await CloakProxyBridge.start(
            f"socks5h://account-user:secret@127.0.0.1:{final_port}",
            host_proxy=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}",
        )
        self.bridges.append(bridge)
        reader, writer = await self.connect(bridge)
        writer.write(b"CONNECT google.invalid:443 HTTP/1.1\r\nHost: google.invalid:443\r\n\r\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), 3)
        self.assertNotIn(b"200", response)
        self.assertFalse(final_seen)

    async def test_backend_keeps_original_binding_and_preserves_failed_close_for_retry(self):
        context = SimpleNamespace(close=AsyncMock())
        relay = SimpleNamespace(url="http://127.0.0.1:12345", close=AsyncMock())
        launch = AsyncMock(return_value=context)
        fake_cloak = SimpleNamespace(launch_persistent_context_async=launch)
        with TemporaryDirectory() as root:
            profile = Path(root)
            (profile / "metadata.json").write_text('{"fingerprint_seed":12345}', encoding="utf-8")
            backend = CloakBrowserBackend()
            original = "socks5h://user:secret@proxy.invalid:1234"
            options = backend._launch_options(profile, original)
            canonical = hashlib.sha256(
                json.dumps(
                    {key: value for key, value in options.items() if key != "headless"},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            with (
                patch.dict(sys.modules, {"cloakbrowser": fake_cloak}),
                patch.object(CloakProxyBridge, "start", AsyncMock(return_value=relay)),
            ):
                self.assertIs(await backend.open(profile, proxy_url=original), context)
            self.assertEqual(launch.call_args.kwargs["proxy"], relay.url)
            self.assertEqual((profile / "launch_settings.sha256").read_text(), canonical)
            context.close.side_effect = RuntimeError("still open")
            with self.assertRaisesRegex(RuntimeError, "still open"):
                await backend.close(context)
            relay.close.assert_not_awaited()
            context.close.side_effect = None
            await backend.close(context)
            relay.close.assert_awaited_once()
            self.assertFalse(backend._proxy_bridges)

    async def test_backend_launch_failure_closes_bridge(self):
        relay = SimpleNamespace(url="http://127.0.0.1:12345", close=AsyncMock())
        launch = AsyncMock(side_effect=RuntimeError("launch failed"))
        with TemporaryDirectory() as root:
            profile = Path(root)
            (profile / "metadata.json").write_text('{"fingerprint_seed":12345}', encoding="utf-8")
            with (
                patch.dict(
                    sys.modules,
                    {"cloakbrowser": SimpleNamespace(launch_persistent_context_async=launch)},
                ),
                patch.object(CloakProxyBridge, "start", AsyncMock(return_value=relay)),
            ):
                with self.assertRaisesRegex(RuntimeError, "launch failed"):
                    await CloakBrowserBackend().open(
                        profile, proxy_url="socks5://user:secret@proxy.invalid:1234"
                    )
            relay.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
