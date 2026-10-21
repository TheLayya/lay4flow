"""Keep Chromium and its shared-cookie API client on the same fixed proxy."""

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import unquote, urlsplit


@asynccontextmanager
async def transport_proxy(proxy_url: str | None, *, enabled: bool = True):
    """Apply the host transport hop without changing the logical account exit."""
    host_proxy = os.getenv("LAY4FLOW_HOST_PROXY", "").strip() if enabled else ""
    if not host_proxy:
        yield proxy_url
        return
    # With no account proxy this is an ordinary Windows system proxy. With
    # a fixed account proxy it must connect through that final proxy instead.
    bridge = await CloakProxyBridge.start(
        proxy_url or host_proxy, host_proxy=host_proxy if proxy_url else ""
    )
    try:
        yield bridge.url
    finally:
        await bridge.close()


class CloakProxyBridge:
    """An in-process, loopback-only HTTP bridge to one account's fixed proxy."""

    def __init__(self) -> None:
        self.server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._handlers: set[asyncio.Task] = set()

    @classmethod
    async def start(cls, proxy_url: str, *, host_proxy: str | None = None) -> "CloakProxyBridge":
        """Start the account-local HTTP bridge.

        ``proxy_url`` remains the account's fixed egress and is always the
        final hop.  A host proxy is only a transport hop for Docker Desktop
        networks (for example ``http://host.docker.internal:7897``); it does
        not replace, or get included in, the account binding hash.  The
        environment variable is intentionally read here, at bridge creation,
        so browser sessions and HTTP clients use the same chain.
        """
        import pproxy

        parsed = urlsplit(proxy_url)
        scheme = {"socks5h": "socks5", "https": "http+secure"}.get(parsed.scheme, parsed.scheme)
        if scheme not in {"socks5", "http", "http+secure"} or not parsed.hostname:
            raise ValueError("invalid fixed browser proxy")
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        remote = pproxy.Connection(f"{scheme}://{host}:{parsed.port}")
        # pproxy's URI fragment authentication syntax conflicts with URL-encoded
        # credentials. Pass decoded credentials directly, never through its URI.
        if parsed.username is not None:
            remote.users = [f"{unquote(parsed.username)}:{unquote(parsed.password or '')}".encode()]
        bridge = cls()
        host_proxy = (
            os.getenv("LAY4FLOW_HOST_PROXY", "") if host_proxy is None else host_proxy
        ).strip()
        upstream = remote
        if host_proxy:
            host = urlsplit(host_proxy)
            host_scheme = {"socks5h": "socks5", "https": "http+secure"}.get(
                host.scheme, host.scheme
            )
            if host_scheme not in {"socks5", "http", "http+secure"} or not host.hostname:
                raise ValueError("invalid host proxy chain")
            # Link the transport hop explicitly instead of embedding account
            # credentials in a URI fragment.  Fragments cannot represent
            # passwords containing '#', '/', or percent escapes reliably.
            first_host = f"[{host.hostname}]" if ":" in host.hostname else host.hostname
            first = pproxy.Connection(f"{host_scheme}://{first_host}:{host.port}")
            if host.username is not None:
                first.users = [f"{unquote(host.username)}:{unquote(host.password or '')}".encode()]
            first.jump = remote
            upstream = first
        original_open = upstream.open_connection

        async def open_connection(*args: Any, **kwargs: Any):
            reader, writer = await original_open(*args, **kwargs)
            bridge._track_writer(writer)
            return reader, writer

        upstream.open_connection = open_connection

        async def handle(reader, writer, **kwargs):
            task = asyncio.current_task()
            bridge._handlers.add(task)
            bridge._track_writer(writer)
            try:
                await pproxy.server.stream_handler(reader, writer, **kwargs)
            finally:
                bridge._handlers.discard(task)

        bridge.server = await pproxy.Server("http://127.0.0.1:0").start_server(
            {"rserver": [upstream]}, stream_handler=handle
        )
        return bridge

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    def _track_writer(self, writer: asyncio.StreamWriter) -> None:
        self._writers = {item for item in self._writers if not item.is_closing()}
        self._writers.add(writer)

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
        handlers = list(self._handlers)
        for task in handlers:
            task.cancel()
        writers, self._writers = self._writers, set()
        for writer in writers:
            writer.close()
        # Python 3.12's Server.wait_closed also waits for accepted connections.
        # Close sockets first, including handshakes waiting on a dead upstream.
        try:
            async with asyncio.timeout(3):
                await asyncio.gather(*handlers, return_exceptions=True)
                await asyncio.gather(
                    *(writer.wait_closed() for writer in writers), return_exceptions=True
                )
                if self.server is not None:
                    await self.server.wait_closed()
        except TimeoutError:
            for writer in writers:
                writer.transport.abort()
