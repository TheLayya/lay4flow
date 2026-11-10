"""Synthetic Vids rules and final receipt classification, independent of Flow."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.core.database import Database
from src.core.models import Token
from src.services.account_error_policy import AccountErrorPolicy, PolicyValidationError
from src.services.vids.account_policy import VidsAccountPolicy


class VidsAccountPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_between_two_out_of_order_failures_counts_only_later_fault(self):
        await self.policy.save_config({"consecutive_failure_threshold": 3})
        with patch("src.core.storage.vids_outcomes.time.time", return_value=1000):
            for key, changes in (
                ("first", {"updated_at": 800}),
                ("third", {"updated_at": 950}),
                ("middle", {"updated_at": 900, "status": "completed"}),
            ):
                await self.policy.observe(
                    self.token_id, self.receipt(key, **changes), request_summary={}
                )
            self.assertEqual(
                (await self.policy.account_stats(self.token_id))["consecutive_error_count"], 1
            )
            await self.policy.observe(
                self.token_id, self.receipt("fourth", updated_at=980), request_summary={}
            )
            self.assertEqual(await self.policy.public_states(), {})

    async def test_late_older_outcomes_do_not_replace_newer_protection_window(self):
        await self.policy.save_config({"consecutive_failure_threshold": 1})
        with patch("src.core.storage.vids_outcomes.time.time", return_value=1000):
            await self.policy.observe(
                self.token_id,
                self.receipt("new-success", status="completed", updated_at=900),
                request_summary={},
            )
            await self.policy.observe(
                self.token_id, self.receipt("older-failure", updated_at=800), request_summary={}
            )
            self.assertEqual(await self.policy.public_states(), {})
            self.assertEqual(
                (await self.policy.account_stats(self.token_id))["consecutive_error_count"], 0
            )
            await self.policy.observe(
                self.token_id, self.receipt("new-failure", updated_at=950), request_summary={}
            )
            self.assertIsNotNone(await self.policy.availability(self.token_id))
            await self.policy.observe(
                self.token_id,
                self.receipt("older-success", status="completed", updated_at=920),
                request_summary={},
            )
            self.assertIsNotNone(await self.policy.availability(self.token_id))
            self.assertEqual(
                (await self.policy.account_stats(self.token_id))["consecutive_error_count"], 1
            )

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / "policy.db")
        self.db = Database(self.path)
        await self.db.init_db()
        self.token_id = await self.db.add_token(
            Token(st="synthetic-policy", email="vids@example.invalid")
        )
        self.policy = VidsAccountPolicy(self.db)

    def receipt(self, key="request", **changes):
        return {
            "id": "vids_" + hashlib.sha256(key.encode()).hexdigest(),
            "kind": "image",
            "status": "failed",
            "created_at": 100,
            "error": "upstream_http_403",
            "error_operation": "generate",
            **changes,
        }

    async def test_config_validation_persistence_and_flow_independence(self):
        settings = await self.policy.get_config()
        self.assertEqual(settings["keepalive_interval_minutes"], 10)
        values = {
            "keepalive_enabled": False,
            "keepalive_interval_minutes": 25,
            "repair_cooldown_seconds": 600,
            "consecutive_failure_threshold": 4,
            "failure_cooldown_seconds": 20,
            "rules": "SYNTHETIC_DENIED|DISABLE",
        }
        self.assertEqual(
            {key: (await self.policy.save_config(values))[key] for key in values}, values
        )
        restored = VidsAccountPolicy(Database(self.path))
        self.assertEqual({key: (await restored.get_config())[key] for key in values}, values)
        flow = await AccountErrorPolicy(self.db).get_config()
        self.assertEqual(flow["consecutive_failure_threshold"], 3)
        self.assertNotIn("SYNTHETIC_DENIED", flow["rules"])
        before = await self.policy.get_config()
        for invalid in (
            {"unknown": 1},
            {"keepalive_enabled": 1},
            {"keepalive_interval_minutes": 0},
            {"repair_cooldown_seconds": 29},
            {"consecutive_failure_threshold": True},
            {"failure_cooldown_seconds": 86401},
            {"rules": "invalid"},
        ):
            with self.assertRaises(PolicyValidationError):
                await self.policy.save_config(invalid)
        self.assertEqual(await self.policy.get_config(), before)

    async def test_classifier_excludes_upload_probe_download_and_unknown_faults(self):
        for changes in (
            {"status": "unknown"},
            {"status": "download_failed"},
            {"operation": "insert"},
        ):
            self.assertIsNone(self.policy.classify(self.receipt(**changes)))
        self.assertEqual(self.policy.classify(self.receipt()), "failure")
        self.assertEqual(self.policy.classify(self.receipt(status="completed")), "success")

    async def test_structured_error_rule_applies_once_without_storing_raw_provider_fields(self):
        await self.policy.save_config({"rules": "SYNTHETIC_DENIED|DEACTIVE"})
        receipt = self.receipt(
            upstream_error={"status": "INVALID_ARGUMENT", "reason": "SYNTHETIC_DENIED"},
            response={"secret": "private provider response"},
        )
        self.assertTrue(
            (await self.policy.observe(self.token_id, receipt, request_summary={}))["settled"]
        )
        self.assertFalse((await self.policy.observe(self.token_id, receipt))["settled"])
        state = (await self.policy.public_states())[self.token_id]
        self.assertEqual((state["action"], state["reason"]), ("DEACTIVE", "SYNTHETIC_DENIED"))
        self.assertIsNotNone(await self.policy.availability(self.token_id))
        self.assertEqual((await self.policy.account_stats(self.token_id))["error_count"], 1)
        log = (await self.db.get_logs(include_payload=True))[0]
        self.assertNotIn("private", log["response_body"])
        self.assertIn("SYNTHETIC_DENIED", log["response_body"])
        payload = json.loads(log["response_body"])
        self.assertEqual(
            payload["upstream_error"],
            {"status": "INVALID_ARGUMENT", "reason": "SYNTHETIC_DENIED"},
        )
        self.assertTrue((await self.db.get_token(self.token_id)).is_active)
        await self.policy.clear(self.token_id)
        self.assertEqual(await self.policy.public_states(), {})

    async def test_threshold_uses_only_final_paid_failures_success_resets_window(self):
        await self.policy.save_config({"consecutive_failure_threshold": 2})
        await self.policy.observe(self.token_id, self.receipt("first"), request_summary={})
        await self.policy.observe(
            self.token_id, self.receipt("unknown", status="unknown"), request_summary={}
        )
        await self.policy.observe(
            self.token_id, self.receipt("download", status="download_failed"), request_summary={}
        )
        self.assertEqual(await self.policy.public_states(), {})
        await self.policy.observe(
            self.token_id, self.receipt("success", status="completed"), request_summary={}
        )
        await self.policy.observe(self.token_id, self.receipt("second"), request_summary={})
        self.assertEqual(await self.policy.public_states(), {})
        await self.policy.observe(self.token_id, self.receipt("third"), request_summary={})
        self.assertEqual((await self.policy.public_states())[self.token_id]["status"], "cooldown")
        stats = await self.policy.account_stats(self.token_id)
        self.assertEqual(
            (stats["success_count"], stats["error_count"], stats["consecutive_error_count"]),
            (1, 3, 2),
        )

    async def test_retry_actions_rejected_and_zero_limit_still_counts_known_failure(self):
        for rule in ("upstream_http_400|RETRY", "upstream_http_400|DELAY|1s"):
            with self.assertRaises(PolicyValidationError):
                await self.policy.save_config({"rules": rule})
        await self.policy.save_config(
            {"rules": "upstream_http_403|LIMIT|0s", "consecutive_failure_threshold": 1}
        )
        await self.policy.observe(self.token_id, self.receipt(), request_summary={})
        self.assertEqual((await self.policy.account_stats(self.token_id))["error_count"], 1)
        self.assertEqual((await self.policy.public_states())[self.token_id]["action"], "LIMIT")

    async def test_local_reconcile_never_imports_historical_usage_or_faults(self):
        root = Path(self.directory.name) / "receipts"
        root.mkdir()
        # Creation order and filename order intentionally differ from outcome order.
        records = [
            self.receipt("last", created_at=300),
            self.receipt("first", created_at=100),
            self.receipt("middle", status="completed", created_at=200),
        ]
        for record in records:
            (root / (record["id"] + ".json")).write_text(json.dumps(record), encoding="utf-8")
        (root / "vids_invalid.json").write_text("broken", encoding="utf-8")
        await self.policy.reconcile(self.token_id, root)
        self.assertEqual(
            (await self.policy.account_stats(self.token_id))["consecutive_error_count"], 0
        )
        restored = VidsAccountPolicy(Database(self.path))
        await restored.reconcile(self.token_id, root)
        stats = await restored.account_stats(self.token_id)
        self.assertEqual((stats["success_count"], stats["error_count"]), (0, 0))
        self.assertEqual(len(await self.db.get_logs()), 0)

    async def test_parameter_and_media_failures_are_logged_without_account_strikes(self):
        for index, changes in enumerate(
            (
                {"error": "upstream_http_400"},
                {"error": "upstream_http_private"},
                {"status": "no_result"},
                {"status": "specification_mismatch"},
                {"error_operation": "upload-start"},
                {"error": "session_unavailable", "error_operation": None},
            )
        ):
            await self.policy.observe(
                self.token_id, self.receipt(str(index), **changes), request_summary={}
            )
        stats = await self.policy.account_stats(self.token_id)
        self.assertEqual((stats["error_count"], stats["consecutive_error_count"]), (6, 0))
        self.assertEqual(await self.policy.public_states(), {})

    async def test_cross_product_reason_uses_existing_threshold_and_editable_rule_only(self):
        reason = {"status": "PERMISSION_DENIED", "reason": "CROSS_PRODUCT_SHARING_DISABLED"}

        def receipt(key):
            return self.receipt(key, upstream_error=reason)

        await self.policy.save_config({"consecutive_failure_threshold": 3, "rules": ""})
        await self.policy.observe(self.token_id, receipt("cp1"), request_summary={})
        await self.policy.observe(self.token_id, receipt("cp2"), request_summary={})
        # A single known 403 reason never becomes an implicit permanent disable.
        self.assertEqual(await self.policy.public_states(), {})
        self.assertEqual(
            (await self.policy.account_stats(self.token_id))["consecutive_error_count"], 2
        )
        await self.policy.observe(self.token_id, receipt("cp3"), request_summary={})
        state = (await self.policy.public_states())[self.token_id]
        self.assertEqual(state["status"], "cooldown")
        # An explicit editable rule can still classify the same safe reason.
        await self.policy.clear(self.token_id)
        await self.policy.save_config(
            {
                "rules": "CROSS_PRODUCT_SHARING_DISABLED|LIMIT|5m",
                "consecutive_failure_threshold": 99,
            }
        )
        await self.policy.observe(self.token_id, receipt("cp4"), request_summary={})
        state = (await self.policy.public_states())[self.token_id]
        self.assertEqual((state["action"], state["reason"]), ("LIMIT", reason["reason"]))


if __name__ == "__main__":
    unittest.main()
