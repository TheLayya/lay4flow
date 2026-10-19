"""Offline UA/TLS selection checks against the pinned transport capabilities."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.services.flow.fingerprint import resolve_browser_impersonate


class TransportIdentityTests(unittest.TestCase):
    def test_supported_chrome_releases_match_exactly(self):
        for major in (120, 124, 133, 146, 150):
            with self.subTest(major=major):
                expected = "chrome133a" if major == 133 else f"chrome{major}"
                self.assertEqual(
                    resolve_browser_impersonate(f"Mozilla/5.0 Chrome/{major}.0.0.0 Safari/537.36"),
                    expected,
                )

    def test_unsupported_chrome_uses_closest_older_profile(self):
        for major, expected in ((132, "chrome131"), (147, "chrome146"), (151, "chrome150")):
            with self.subTest(major=major):
                self.assertEqual(resolve_browser_impersonate(f"Chromium/{major}.0"), expected)

    def test_missing_or_synthetic_ua_has_a_fixed_default(self):
        for ua in ("", "Synthetic browser UA", "Mozilla/5.0"):
            with self.subTest(ua=ua):
                self.assertEqual(resolve_browser_impersonate(ua), "chrome150")
        self.assertEqual(
            resolve_browser_impersonate("Synthetic UA", fallback="chrome120"), "chrome120"
        )

    def test_existing_non_desktop_families_remain_separate(self):
        examples = (
            ("Mozilla/5.0 Android Chrome/150.0 Safari/537.36", "chrome_android"),
            ("Mozilla/5.0 Chrome/150.0 Safari/537.36 Edg/150.0", "edge"),
            ("Mozilla/5.0 Version/17.0 Safari/605.1.15", "safari"),
            ("Mozilla/5.0 (iPhone) Version/17.0 Safari/604.1", "safari_ios"),
        )
        for ua, expected in examples:
            with self.subTest(ua=ua):
                self.assertEqual(resolve_browser_impersonate(ua), expected)

    def test_outdated_transport_fails_instead_of_silently_using_124(self):
        old_targets = [SimpleNamespace(value="chrome124")]
        with patch("curl_cffi.requests.impersonate.BrowserType", old_targets):
            with self.assertRaisesRegex(RuntimeError, "curl-cffi==0.16.3"):
                resolve_browser_impersonate("Chrome/150.0.0.0")


if __name__ == "__main__":
    unittest.main()
