"""Pure cache rules preserve naming, headers, and user-facing errors."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.services.cache import rules
from src.services.file_cache import FileCache


class CacheRulesTests(unittest.TestCase):
    def test_extensions_and_cache_names_match_legacy_contract(self):
        cases = [
            ("https://example.test/a.mp4?sig=x", "video", ".mp4"),
            ("https://example.test/a.webm", "video", ".webm"),
            ("https://example.test/a.png", "image", ".png"),
            ("https://example.test/a", "image", ".jpg"),
            ("https://example.test/a.bin", "other", ".bin"),
        ]
        cache = FileCache.__new__(FileCache)
        for url, media_type, extension in cases:
            with self.subTest(url=url, media_type=media_type):
                self.assertEqual(cache._guess_extension(url, media_type), extension)
                self.assertEqual(
                    cache._generate_cache_filename(url, media_type),
                    rules.generate_cache_filename(url, media_type),
                )

    def test_headers_keep_fingerprint_values_and_media_dest(self):
        image = rules.build_download_headers(
            "image",
            {
                "user_agent": "remote-agent",
                "accept_language": "en-US",
                "sec_ch_ua": '"Brand";v="1"',
                "sec_ch_ua_mobile": "?0",
                "sec_ch_ua_platform": '"Windows"',
            },
        )
        self.assertEqual(image["User-Agent"], "remote-agent")
        self.assertEqual(image["Accept-Language"], "en-US")
        self.assertEqual(image["Sec-Fetch-Dest"], "image")
        self.assertEqual(image["sec-ch-ua"], '"Brand";v="1"')
        self.assertEqual(rules.build_download_headers("video")["Sec-Fetch-Dest"], "video")

    def test_error_normalization_is_identical_through_legacy_wrapper(self):
        cache = FileCache.__new__(FileCache)
        errors = [
            RuntimeError("Failed to cache file: bad"),
            RuntimeError(""),
            FileNotFoundError("wget"),
        ]
        for error in errors:
            self.assertEqual(
                cache._normalize_cache_error(error), rules.normalize_cache_error(error)
            )


class MediaPoolDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_media_pool_overrides_fingerprint_and_keeps_exit_across_retry_tools(self):
        proxy = SimpleNamespace(
            select_media_proxy=AsyncMock(return_value="http://media-pool.proxy:8080"),
            get_request_proxy_url=AsyncMock(),
            get_media_proxy_url=AsyncMock(),
        )
        flow = SimpleNamespace(
            get_request_fingerprint=lambda: {"proxy_url": "http://account.proxy:8080"}
        )
        with tempfile.TemporaryDirectory() as directory:
            cache = FileCache(directory, proxy_manager=proxy, flow_client=flow)
            session = MagicMock()
            session.__aenter__ = AsyncMock(return_value=session)
            session.__aexit__ = AsyncMock(return_value=False)
            session.get = AsyncMock(side_effect=RuntimeError("synthetic transport failure"))
            commands = []

            def run(command, **kwargs):
                commands.append((command, kwargs))
                if command[0] == "wget":
                    return SimpleNamespace(returncode=1, stderr=b"synthetic failure")
                Path(command[command.index("-o") + 1]).write_bytes(b"synthetic image")
                return SimpleNamespace(returncode=0)

            with (
                patch("src.services.file_cache.AsyncSession", return_value=session),
                patch("subprocess.run", side_effect=run),
            ):
                await cache.download_and_cache("https://storage.googleapis.com/result.png", "image")
            proxy.select_media_proxy.assert_awaited_once()
            proxy.get_request_proxy_url.assert_not_awaited()
            proxy.get_media_proxy_url.assert_not_awaited()
            self.assertEqual(session.get.await_args.kwargs["proxy"], "http://media-pool.proxy:8080")
            self.assertEqual(commands[0][1]["env"]["https_proxy"], "http://media-pool.proxy:8080")
            self.assertEqual(
                commands[1][0][commands[1][0].index("-x") + 1], "http://media-pool.proxy:8080"
            )

    async def test_media_pool_failure_never_falls_back_to_account_or_direct(self):
        proxy = SimpleNamespace(select_media_proxy=AsyncMock(side_effect=ValueError("empty pool")))
        with tempfile.TemporaryDirectory() as directory:
            cache = FileCache(directory, proxy_manager=proxy)
            with patch("src.services.file_cache.AsyncSession") as transport:
                with self.assertRaisesRegex(ValueError, "empty pool"):
                    await cache.download_and_cache(
                        "https://storage.googleapis.com/result.png", "image"
                    )
                transport.assert_not_called()

    async def test_host_transport_is_used_by_curl_and_external_downloaders(self):
        proxy = SimpleNamespace(
            select_media_proxy=AsyncMock(return_value="socks5h://u:p@proxy.invalid:1080")
        )
        bridge = SimpleNamespace(url="http://127.0.0.1:19877", close=AsyncMock())
        flow = SimpleNamespace(get_request_fingerprint=lambda: None)
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        session.get = AsyncMock(side_effect=RuntimeError("synthetic transport failure"))
        commands = []

        def run(command, **kwargs):
            commands.append((command, kwargs))
            if command[0] == "wget":
                return SimpleNamespace(returncode=1, stderr=b"synthetic failure")
            Path(command[command.index("-o") + 1]).write_bytes(b"synthetic image")
            return SimpleNamespace(returncode=0)

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict("os.environ", {"LAY4FLOW_HOST_PROXY": "http://host.docker.internal:7897"}),
            patch("src.services.file_cache.AsyncSession", return_value=session),
            patch(
                "src.services.browser.cloak_proxy.CloakProxyBridge.start",
                AsyncMock(return_value=bridge),
            ),
            patch("subprocess.run", side_effect=run),
        ):
            cache = FileCache(directory, proxy_manager=proxy, flow_client=flow)
            await cache.download_and_cache("https://storage.googleapis.com/result.png", "image")
        self.assertEqual(session.get.await_args.kwargs["proxy"], bridge.url)
        self.assertEqual(commands[0][1]["env"]["https_proxy"], bridge.url)
        self.assertEqual(commands[1][0][commands[1][0].index("-x") + 1], bridge.url)
        self.assertEqual(bridge.close.await_count, 3)


if __name__ == "__main__":
    unittest.main()
