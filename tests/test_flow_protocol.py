"""Protocol contracts run without config changes, HTTP, or a captcha service."""

import subprocess
import sys
import unittest
from pathlib import Path

from src.services.flow.fingerprint import (
    apply_remote_fingerprint_headers,
    normalize_remote_fingerprint,
    redact_credentials,
    summarize_remote_http_error,
)
from src.services.flow.video_edit import video_edit_frame_range


class FlowProtocolTests(unittest.TestCase):
    def test_pure_components_do_not_load_application_or_transport(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.modules['src.core.config']=None; sys.modules['curl_cffi']=None; "
                "from src.services.flow.video_edit import video_edit_frame_range; "
                "from src.services.flow.fingerprint import normalize_remote_fingerprint; "
                "assert 'src.services.generation_handler' not in sys.modules",
            ],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_legacy_service_exports_keep_class_identity(self):
        from src.services import FlowClient
        from src.services.flow_client import FlowClient as DirectFlowClient

        self.assertIs(FlowClient, DirectFlowClient)

    def test_solve_identity_is_scoped_and_keeps_explicit_credentials(self):
        fingerprint = normalize_remote_fingerprint(
            {
                "fingerprint": {"user_agent": "remote-agent", "accept_language": ""},
                "accept_language": "en-GB",
                "proxy_url": "http://ignored.invalid",
                "session_cookies": [
                    {"name": "SID", "value": "solver", "domain": ".google.com", "path": "/"},
                    {
                        "name": "EXPIRED",
                        "value": "old",
                        "domain": ".google.com",
                        "path": "/",
                        "expires": 99,
                    },
                    {
                        "name": "PATH",
                        "value": "scoped",
                        "domain": ".google.com",
                        "path": "/project",
                    },
                ],
            },
            project_id="p",
            origin="https://flow.google.com",
            referer="https://flow.google.com/project/p",
        )
        self.assertNotIn("proxy_url", fingerprint)
        headers = {
            "cookie": "SID=account",
            "Authorization": "Bearer account",
            "sec-ch-ua": "inferred",
        }
        apply_remote_fingerprint_headers(
            headers,
            "https://flow.google.com/project/p",
            fingerprint,
            fallback_user_agent="fallback",
            now=100,
        )
        self.assertEqual(
            headers,
            {
                "User-Agent": "remote-agent",
                "Accept-Language": "en-GB",
                "Cookie": "SID=account; PATH=scoped",
                "Authorization": "Bearer account",
            },
        )
        unrelated_headers = {}
        apply_remote_fingerprint_headers(
            unrelated_headers,
            "https://unrelated.invalid/project/p",
            fingerprint,
            fallback_user_agent="fallback",
            now=100,
        )
        self.assertNotIn("Cookie", unrelated_headers)

    def test_redaction_keeps_retry_markers_and_drops_credential_echoes(self):
        payload = {"nested": [{"token": "secret", "session_cookies": ["secret"]}], "ok": True}
        self.assertEqual(
            redact_credentials(payload),
            {
                "nested": [{"token": "<redacted>", "session_cookies": "<redacted>"}],
                "ok": True,
            },
        )
        self.assertEqual(payload["nested"][0]["token"], "secret")
        self.assertEqual(
            summarize_remote_http_error(403, "reCAPTCHA evaluation failed: secret"),
            "HTTP Error 403: reCAPTCHA evaluation failed",
        )

    def test_edit_frames_follow_processed_asset_type_and_duration(self):
        for asset_type, expected_end in (("userUploadedVideo", 160), ("generatedVideo", 128)):
            with self.subTest(asset_type=asset_type):
                media = {"video": {asset_type: {}, "dimensions": {"length": "5.330s"}}}
                self.assertEqual(
                    video_edit_frame_range(media, 0, 10000),
                    {
                        "startFrameIndex": 0,
                        "endFrameIndex": expected_end,
                    },
                )


if __name__ == "__main__":
    unittest.main()
