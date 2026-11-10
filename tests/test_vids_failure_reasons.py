"""Safe Vids refusal classification: hints must not cross generator boundaries."""

import unittest

from src.services.vids.gateway import (
    CROSS_PRODUCT_HINT,
    SPEECH_LANGUAGE_HINT,
    safe_upstream_error,
    upstream_failure_text,
)


def record(kind, status, reason, **extra):
    return {
        "kind": kind,
        "upstream_error": {"status": status, "reason": reason},
        **extra,
    }


class VidsFailureReasonTests(unittest.TestCase):
    def test_language_hint_is_limited_to_speech(self):
        for kind in ("image", "video", "music", None):
            with self.subTest(kind=kind):
                text = upstream_failure_text(
                    record(kind, "INVALID_ARGUMENT", "LANGUAGE_NOT_SUPPORTED")
                )
                self.assertEqual(text, "LANGUAGE_NOT_SUPPORTED")
                self.assertNotIn(SPEECH_LANGUAGE_HINT, text)
        speech = upstream_failure_text(
            record("speech", "INVALID_ARGUMENT", "LANGUAGE_NOT_SUPPORTED")
        )
        self.assertIn("LANGUAGE_NOT_SUPPORTED", speech)
        self.assertIn(SPEECH_LANGUAGE_HINT, speech)

    def test_cross_product_hint_is_not_language_or_kind_specific(self):
        for kind in ("speech", "image", "video"):
            with self.subTest(kind=kind):
                text = upstream_failure_text(
                    record(kind, "PERMISSION_DENIED", "CROSS_PRODUCT_SHARING_DISABLED")
                )
                self.assertIn("CROSS_PRODUCT_SHARING_DISABLED", text)
                self.assertIn(CROSS_PRODUCT_HINT, text)
                self.assertNotIn(SPEECH_LANGUAGE_HINT, text)

    def test_safe_upstream_error_drops_raw_provider_content(self):
        value = {
            "status": "INVALID_ARGUMENT",
            "reason": "LANGUAGE_NOT_SUPPORTED",
            "message": "private prompt https://private.invalid token=secret",
            "metadata": {"cookie": "private-cookie"},
        }
        self.assertEqual(
            safe_upstream_error({"upstream_error": value}),
            {"status": "INVALID_ARGUMENT", "reason": "LANGUAGE_NOT_SUPPORTED"},
        )
        text = upstream_failure_text({"kind": "speech", "upstream_error": value})
        self.assertNotIn("private", text)
        self.assertEqual(
            safe_upstream_error(
                {"upstream_error": {"status": "lower", "reason": "LANGUAGE_NOT_SUPPORTED"}}
            ),
            {"reason": "LANGUAGE_NOT_SUPPORTED"},
        )
        self.assertEqual(
            safe_upstream_error(
                {"upstream_error": {"status": "INVALID_ARGUMENT", "reason": "lower"}}
            ),
            {"status": "INVALID_ARGUMENT"},
        )
        for invalid in ({"status": "lower", "reason": "lower"}, None, "private"):
            self.assertIsNone(safe_upstream_error({"upstream_error": invalid}))


if __name__ == "__main__":
    unittest.main()
