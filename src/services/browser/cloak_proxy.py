"""Keep Chromium and its shared-cookie API client on the same fixed proxy."""

import asyncio
from typing import Any
from urllib.parse import unquote, urlsplit


class CloakProxyBridge:
    """An in-process, loopback-only HTTP bridge to one account's fixed proxy."""

    def __init__(self) -> None:
        self.server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._handlers: set[asyncio.Task] = set()

    @classmethod
    async def start(cls, proxy_url: str) -> "CloakProxyBridge":
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
        original_open = remote.open_connection

        async def open_connection(*args: Any, **kwargs: Any):
            reader, writer = await original_open(*args, **kwargs)
            bridge._track_writer(writer)
            return reader, writer

        remote.open_connection = open_connection

        async def handle(reader, writer, **kwargs):
            task = asyncio.current_task()
            bridge._handlers.add(task)
            bridge._track_writer(writer)
            try:
                await pproxy.server.stream_handler(reader, writer, **kwargs)
            finally:
                bridge._handlers.discard(task)

        bridge.server = await pproxy.Server("http://127.0.0.1:0").start_server(
            {"rserver": [remote]}, stream_handler=handle
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
