"""Typed remote transport diagnostics never expose exceptions or replay a POST."""

import asyncio
import json
import socket
import ssl
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from src.services.flow.remote_captcha import (
    RemoteCaptchaError,
    check_remote_response,
    transport_error_reason,
)
from src.services.flow_client import FlowClient
from src.services.generation_attempts import GenerationAttempts

SECRET = "Cookie=synthetic-secret; Bearer synthetic-key https://user:password@private.invalid"


class RemoteTransportClassificationTests(unittest.TestCase):
    def test_tls_handshake_wait_nested_inside_connect_timeout_is_not_a_tls_failure(self):
        handshake_wait = ssl.SSLWantReadError(2, SECRET)
        timeout = httpx.ConnectTimeout(SECRET)
        timeout.__cause__ = handshake_wait
        self.assertEqual(transport_error_reason(timeout), "CONNECT_TIMEOUT")

    def test_connect_wait_is_bounded_by_request_timeout_and_ten_seconds(self):
        for configured, connect in ((3, 3), (5, 5), (35, 10), (45, 10), (60, 10)):
            with self.subTest(configured=configured):
                timeout = FlowClient._build_remote_browser_http_timeout(configured)
                self.assertEqual(timeout.connect, connect)
                self.assertEqual(timeout.read, configured)
                self.assertEqual(timeout.pool, 2.5)

    def test_specific_httpx_reasons_and_fixed_descriptions_exclude_raw_messages(self):
        for error_type, reason in (
            (httpx.ConnectTimeout, "CONNECT_TIMEOUT"),
            (httpx.ReadTimeout, "READ_TIMEOUT"),
            (httpx.WriteTimeout, "WRITE_TIMEOUT"),
            (httpx.PoolTimeout, "POOL_TIMEOUT"),
            (httpx.RemoteProtocolError, "PROTOCOL_ERROR"),
            (httpx.ConnectError, "CONNECTION_ERROR"),
            (httpx.InvalidURL, "INVALID_URL"),
        ):
            with self.subTest(reason=reason):
                error = RemoteCaptchaError.from_transport(error_type(SECRET))
                self.assertEqual((error.code, error.reason), ("TRANSPORT_ERROR", reason))
                self.assertIn(reason, str(error))
                self.assertNotIn("synthetic", str(error))
                self.assertNotIn("private.invalid", str(error))
                self.assertNotIn("password", str(error))

    def test_nested_dns_tls_and_os_causes_are_more_precise_than_wrapper(self):
        for cause, reason in (
            (socket.gaierror(socket.EAI_NONAME, SECRET), "DNS_ERROR"),
            (ssl.SSLCertVerificationError(1, SECRET), "TLS_CERTIFICATE_ERROR"),
            (ssl.SSLError(1, SECRET), "TLS_ERROR"),
            (ConnectionRefusedError(111, SECRET), "CONNECTION_REFUSED"),
            (ConnectionResetError(104, SECRET), "CONNECTION_RESET"),
        ):
            for outer in (httpx.ConnectError(SECRET), urllib.error.URLError(cause)):
                with self.subTest(wrapper=type(outer).__name__, reason=reason):
                    outer.__cause__ = cause
                    self.assertEqual(transport_error_reason(outer), reason)
                    self.assertNotIn("synthetic", str(RemoteCaptchaError.from_transport(outer)))

    def test_unknown_and_cyclic_exception_causes_are_bounded_and_not_guessed_from_text(self):
        first, second = RuntimeError("ConnectTimeout " + SECRET), ValueError(SECRET)
        first.__cause__, second.__cause__ = second, first
        error = RemoteCaptchaError.from_transport(first)
        self.assertEqual(error.reason, "UNKNOWN_TRANSPORT")
        self.assertNotIn("synthetic", str(error))
        self.assertEqual(transport_error_reason(TimeoutError(SECRET)), "TIMEOUT")

    def test_untrusted_reason_and_static_configuration_failures_are_not_retried(self):
        error = RemoteCaptchaError("TRANSPORT_ERROR", transport_reason=SECRET)
        self.assertEqual(error.reason, "UNKNOWN_TRANSPORT")
        self.assertNotIn("synthetic", str(error))
        for cause in (ssl.SSLCertVerificationError(1, SECRET), httpx.InvalidURL(SECRET)):
            self.assertFalse(RemoteCaptchaError.from_transport(cause).retryable)
        self.assertTrue(RemoteCaptchaError.from_transport(httpx.ConnectTimeout(SECRET)).retryable)

    def test_http_status_and_invalid_json_response_remain_distinct(self):
        with self.assertRaises(RemoteCaptchaError) as caught:
            check_remote_response(None, 503)
        self.assertEqual(
            (caught.exception.code, caught.exception.http_status), ("SOLVE_REJECTED", 503)
        )
        with self.assertRaises(RemoteCaptchaError) as caught:
            check_remote_response(None, 200)
        self.assertEqual(caught.exception.code, "INVALID_RESPONSE")
        error = RemoteCaptchaError("ERROR_NO_SLOT_AVAILABLE", http_status=429)
        self.assertIs(RemoteCaptchaError.from_transport(error), error)

    def test_attempt_history_keeps_safe_reason_and_elapsed_stage(self):
        attempts = GenerationAttempts(3)
        attempts.select_account(SimpleNamespace(id=1, email="synthetic@example.test"))
        attempts.take()
        attempts.finish("failed", RemoteCaptchaError.from_transport(httpx.ConnectTimeout(SECRET)))
        row = attempts.snapshot()["attempts"][0]
        self.assertEqual(row["stage"], "captcha")
        self.assertEqual(row["error_code"], "CONNECT_TIMEOUT")
        self.assertIn("连接远程打码服务超时", row["error"])
        self.assertNotIn("synthetic-secret", json.dumps(row))


class RemoteTransportClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = FlowClient(None)
        self.client._get_remote_browser_service_config = Mock(
            return_value=("https://synthetic.invalid", "synthetic-owner-secret", 60)
        )

    async def test_call_keeps_typed_reason_one_post_and_safe_private_diagnostic(self):
        self.client._sync_json_http_request = AsyncMock(side_effect=httpx.ConnectTimeout(SECRET))
        with patch("src.services.flow_client.debug_logger") as logger:
            with self.assertRaises(RemoteCaptchaError) as caught:
                await self.client._get_recaptcha_token("synthetic-project")
        self.assertEqual(caught.exception.reason, "CONNECT_TIMEOUT")
        self.client._sync_json_http_request.assert_awaited_once()
        call = self.client._sync_json_http_request.await_args.kwargs
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["payload"]["clientKey"], "synthetic-owner-secret")
        messages = " ".join(str(call) for call in logger.mock_calls)
        self.assertIn("operation=solve reason=CONNECT_TIMEOUT duration_ms=", messages)
        for secret in (
            "synthetic-owner-secret",
            "synthetic-project",
            "private.invalid",
            "password",
        ):
            self.assertNotIn(secret, messages)
        self.assertIsNone(self.client.get_request_fingerprint())

    async def test_httpx_timeout_is_typed_without_stdlib_fallback_or_environment_proxy(self):
        session = SimpleNamespace(request=AsyncMock(side_effect=httpx.ReadTimeout(SECRET)))
        manager = AsyncMock()
        manager.__aenter__.return_value = session
        with (
            patch("src.services.flow_client.httpx.AsyncClient", return_value=manager) as factory,
            patch.object(FlowClient, "_stdlib_json_http_request", AsyncMock()) as fallback,
        ):
            with self.assertRaises(RemoteCaptchaError) as caught:
                await self.client._sync_json_http_request(
                    "POST", "https://synthetic.invalid/solve", {}, {"clientKey": SECRET}, 45
                )
        factory.assert_called_once_with(follow_redirects=False, trust_env=False)
        session.request.assert_awaited_once()
        fallback.assert_not_awaited()
        self.assertEqual(caught.exception.reason, "READ_TIMEOUT")
        self.assertNotIn("synthetic-secret", str(caught.exception))

    async def test_stdlib_nested_certificate_error_uses_same_bounded_contract(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError(ssl.SSLCertVerificationError(1, SECRET))
        with patch("src.services.flow_client.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(RemoteCaptchaError) as caught:
                await self.client._stdlib_json_http_request(
                    "POST", "https://synthetic.invalid/solve", {}, {"clientKey": SECRET}, 45
                )
        self.assertEqual(caught.exception.reason, "TLS_CERTIFICATE_ERROR")
        opener.open.assert_called_once()
        self.assertFalse(caught.exception.retryable)

    async def test_cancellation_is_not_mislabeled_as_transport_or_replayed(self):
        self.client._sync_json_http_request = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await self.client._get_recaptcha_token("synthetic-project")
        self.client._sync_json_http_request.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
