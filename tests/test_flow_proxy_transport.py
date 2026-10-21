"""Offline transport regressions for account-bound Flow proxy failures."""

import json
import os
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from curl_cffi.requests.exceptions import ProxyError

from src.core.config import config
from src.services.flow.retry import (
    is_proxy_connection_error,
    is_proxy_handshake_error,
    proxy_connection_code,
    should_fallback_to_urllib,
)
from src.services.flow_client import FlowClient

SOCKS_PROXY = "socks5h://account:secret@proxy.example.invalid:1080"
HTTP_PROXY = "http://account:secret@proxy.example.invalid:8080"


class _Response:
    status_code = 200
    headers = {}
    url = "https://flow.google.com/"

    def __init__(self, payload=None):
        self.payload = payload if payload is not None else {"ok": True}
        self.text = json.dumps(self.payload)

    def json(self):
        return self.payload


class _Session:
    def __init__(self, error=None):
        self.outcomes = [error] if error is not None else []
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.outcomes:
            value = self.outcomes.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        return _Response()

    async def post(self, url, **kwargs):
        return await self.get(url, **kwargs)


class FlowProxyTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        example = Path(__file__).resolve().parents[1] / "config" / "setting_example.toml"
        settings = tomllib.loads(example.read_text(encoding="utf-8"))
        settings["flow"]["max_retries"] = 2
        settings["flow"]["image_timeout_retry_count"] = 0
        settings["debug"]["enabled"] = False
        self.enterContext(patch.object(config, "_config", settings))

    def test_real_curl_proxy_error_97_is_classified_without_raw_message(self):
        error = ProxyError(
            "Failed to perform, curl: (97) Unable to complete SOCKS5 connection "
            "to proxy.example.invalid:1080; Cookie=account-secret",
            code=97,
        )

        self.assertTrue(is_proxy_connection_error(error))
        self.assertEqual(proxy_connection_code(error), 97)
        self.assertTrue(is_proxy_handshake_error(error))
        self.assertFalse(should_fallback_to_urllib("proxy handshake error (curl: (97))"))

        client = FlowClient(None)
        safe = client._remote_safe_error(error)
        self.assertEqual(safe, "proxy error: handshake failed (curl: (97))")
        self.assertNotIn("proxy.example.invalid", safe)
        self.assertNotIn("account-secret", safe)
        self.assertTrue(is_proxy_connection_error(ProxyError("", code=97)))
        self.assertTrue(is_proxy_handshake_error(ProxyError("", code=97)))
        self.assertFalse(is_proxy_handshake_error(ProxyError("Proxy CONNECT aborted", code=56)))

    async def test_socks_account_exit_is_fixed_and_never_uses_urllib(self):
        for scheme in ("socks5", "socks5h"):
            for code in (7, 97):
                with self.subTest(scheme=scheme, code=code):
                    bound_proxy = SOCKS_PROXY.replace("socks5h:", scheme + ":")
                    error = ProxyError("Synthetic proxy negotiation failure", code=code)
                    session = _Session(error=error)
                    proxy = SimpleNamespace(
                        get_request_proxy_url=AsyncMock(return_value=HTTP_PROXY)
                    )
                    client = FlowClient(proxy)
                    fallback = Mock(side_effect=AssertionError("SOCKS must not use urllib"))

                    with (
                        patch("src.services.flow_client.AsyncSession", return_value=session),
                        patch.object(client, "_sync_json_request_via_urllib", fallback),
                    ):
                        with client.account_request_proxy(bound_proxy):
                            with self.assertRaisesRegex(Exception, "proxy error: handshake failed"):
                                await client._make_request(
                                    method="GET",
                                    url="https://aisandbox-pa.googleapis.com/v1/credits",
                                    allow_urllib_fallback=True,
                                )

                    self.assertEqual(session.calls[0][1]["proxy"], bound_proxy)
                    proxy.get_request_proxy_url.assert_not_awaited()
                    fallback.assert_not_called()

    async def test_http_proxy_keeps_existing_urllib_fallback(self):
        session = _Session(error=RuntimeError("curl: (7) Failed to connect to proxy"))
        proxy = SimpleNamespace(get_request_proxy_url=AsyncMock(return_value=HTTP_PROXY))
        client = FlowClient(proxy)
        fallback = Mock(return_value={"fallback": True})

        with (
            patch("src.services.flow_client.AsyncSession", return_value=session),
            patch.object(client, "_sync_json_request_via_urllib", fallback),
        ):
            result = await client._make_request(
                method="GET",
                url="https://aisandbox-pa.googleapis.com/v1/credits",
                allow_urllib_fallback=True,
            )

        self.assertEqual(result, {"fallback": True})
        self.assertEqual(session.calls[0][1]["proxy"], HTTP_PROXY)
        fallback.assert_called_once()
        self.assertEqual(fallback.call_args.args[4], HTTP_PROXY)

    async def test_host_proxy_changes_only_transport_not_account_binding(self):
        session = _Session()
        client = FlowClient(None)
        bridge = SimpleNamespace(url="http://127.0.0.1:19876", close=AsyncMock())
        with (
            patch.dict(os.environ, {"LAY4FLOW_HOST_PROXY": "http://host.docker.internal:9786"}),
            patch("src.services.flow_client.AsyncSession", return_value=session),
            patch(
                "src.services.browser.cloak_proxy.CloakProxyBridge.start",
                AsyncMock(return_value=bridge),
            ) as start,
        ):
            with client.account_request_proxy(SOCKS_PROXY):
                await client._make_request("GET", "https://flow.google.com/probe")
                self.assertEqual(client._account_proxy_ctx.get(), SOCKS_PROXY)
                await client._make_text_request("GET", "https://flow.google.com/text")
                await client._make_request(
                    "GET", "https://flow.google.com/direct", force_no_proxy=True
                )
            self.assertEqual(start.await_count, 2)
            self.assertEqual(start.call_args.args, (SOCKS_PROXY,))
            self.assertEqual(
                start.call_args.kwargs["host_proxy"], "http://host.docker.internal:9786"
            )
            self.assertEqual(
                [call[1]["proxy"] for call in session.calls], [bridge.url, bridge.url, None]
            )
            self.assertEqual(bridge.close.await_count, 2)

    async def test_host_proxy_urllib_fallback_keeps_tunnel_alive(self):
        session = _Session(error=RuntimeError("curl: (7) Failed to connect to proxy"))
        client = FlowClient(
            SimpleNamespace(get_request_proxy_url=AsyncMock(return_value=HTTP_PROXY))
        )
        bridge = SimpleNamespace(url="http://127.0.0.1:19876", close=AsyncMock())

        def fallback(*args, **kwargs):
            self.assertEqual(args[4], bridge.url)
            self.assertEqual(bridge.close.await_count, 1)
            return {"fallback": True}

        with (
            patch.dict(os.environ, {"LAY4FLOW_HOST_PROXY": "http://host.docker.internal:9786"}),
            patch("src.services.flow_client.AsyncSession", return_value=session),
            patch(
                "src.services.browser.cloak_proxy.CloakProxyBridge.start",
                AsyncMock(return_value=bridge),
            ),
            patch.object(client, "_sync_json_request_via_urllib", side_effect=fallback),
        ):
            self.assertEqual(
                await client._make_request("GET", "https://flow.google.com/probe"),
                {"fallback": True},
            )
        self.assertEqual(bridge.close.await_count, 2)

    async def test_generation_retry_uses_new_captcha_session_and_finishes_each_one(self):
        proxy = SimpleNamespace(get_request_proxy_url=AsyncMock(return_value=HTTP_PROXY))
        client = FlowClient(proxy)
        session = _Session()
        session.outcomes = [
            ProxyError("Failed to perform, curl: (97) SOCKS handshake failed", code=97),
            _Response({"media": [{"name": "synthetic-media"}]}),
        ]
        solve_number = 0
        callbacks = []

        async def remote_service(**kwargs):
            nonlocal solve_number
            path = kwargs["path"]
            callbacks.append(path)
            if path == "/api/v1/solve":
                solve_number += 1
                return {
                    "token": f"captcha-{solve_number}",
                    "session_id": f"remote-session-{solve_number}",
                    "fingerprint": {
                        "user_agent": f"Synthetic agent {solve_number}",
                        "proxy_url": "http://solver.example.invalid:9999",
                    },
                }
            return {}

        client._call_remote_browser_service = AsyncMock(side_effect=remote_service)
        with (
            patch("src.services.flow_client.AsyncSession", return_value=session),
            patch("src.services.flow_client.asyncio.sleep", new=AsyncMock()),
            patch.object(client, "_sync_json_request_via_urllib") as fallback,
        ):
            with client.account_request_proxy(SOCKS_PROXY):
                result, session_id, trace = await client.generate_image(
                    at="synthetic-at",
                    project_id="synthetic-project",
                    prompt="synthetic prompt",
                    model_name="gemini-3.1-flash-image",
                    aspect_ratio="16:9",
                    token_id=7,
                )

        self.assertEqual(result["media"][0]["name"], "synthetic-media")
        self.assertTrue(session_id.startswith(";"))
        self.assertEqual(len(trace["generation_attempts"]), 2)
        self.assertEqual(
            [request["proxy"] for _, request in session.calls],
            [SOCKS_PROXY, SOCKS_PROXY],
        )
        self.assertEqual(
            [
                request["json"]["clientContext"]["recaptchaContext"]["token"]
                for _, request in session.calls
            ],
            ["captcha-1", "captcha-2"],
        )
        self.assertEqual(
            callbacks,
            [
                "/api/v1/solve",
                "/api/v1/sessions/remote-session-1/error",
                "/api/v1/sessions/remote-session-1/finish",
                "/api/v1/solve",
                "/api/v1/sessions/remote-session-2/finish",
            ],
        )
        proxy.get_request_proxy_url.assert_not_awaited()
        fallback.assert_not_called()

    async def test_failed_generation_retains_every_retry_attempt(self):
        client = FlowClient(None)
        submit = AsyncMock(
            side_effect=RuntimeError(
                "HTTP Error 403: PUBLIC_ERROR_UNUSUAL_ACTIVITY, reCAPTCHA evaluation failed"
            )
        )
        finish = AsyncMock()
        with (
            patch.object(
                client, "_get_recaptcha_token", AsyncMock(return_value=("captcha", "session"))
            ),
            patch.object(client, "_make_image_generation_request", submit),
            patch.object(client, "_notify_browser_captcha_request_finished", finish),
            patch("src.services.flow_client.asyncio.sleep", new=AsyncMock()),
        ):
            with self.assertRaisesRegex(RuntimeError, "PUBLIC_ERROR_UNUSUAL_ACTIVITY") as raised:
                await client.generate_image(
                    at="synthetic-at",
                    project_id="synthetic-project",
                    prompt="synthetic prompt",
                    model_name="gemini-3.1-flash-image",
                    aspect_ratio="16:9",
                )

        trace = raised.exception.flow_retry_trace
        attempts = trace["generation_attempts"]
        self.assertGreaterEqual(len(attempts), 2)
        self.assertEqual(len(attempts), submit.await_count)
        self.assertEqual(finish.await_count, submit.await_count)
        self.assertEqual([item["attempt"] for item in attempts], list(range(1, len(attempts) + 1)))
        self.assertTrue(all(item["success"] is False and item["recaptcha_ok"] for item in attempts))
        self.assertTrue(all("reCAPTCHA evaluation failed" in item["error"] for item in attempts))


if __name__ == "__main__":
    unittest.main()
