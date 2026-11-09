"""Synthetic SQLite acceptance for durable Vids accounting and provider protection."""

import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.core.database import Database
from src.core.models import Token
from src.core.storage.vids_outcomes import VidsOutcomeRepository


class VidsOutcomeStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / "synthetic.db")
        self.db = Database(self.path)
        await self.db.init_db()
        self.token_id = await self.db.add_token(
            Token(st="synthetic-session", email="test@example.invalid")
        )
        self.repository = VidsOutcomeRepository(self.db)

    def receipt(self, key="first", **changes):
        return {
            "id": "vids_" + hashlib.sha256(key.encode()).hexdigest(),
            "kind": "image",
            "status": "completed",
            "created_at": 100,
            "error": None,
            **changes,
        }

    async def test_success_replay_and_restart_count_once_with_one_safe_log(self):
        record = self.receipt(
            response={"credentials": "private-cookie-value"},
            video_request={"prompt": "private prompt"},
            resource={"url": "https://private.invalid/token"},
        )
        first = await self.repository.record_receipt(self.token_id, record, outcome="success")
        self.assertTrue(first["inserted"])
        self.assertTrue(first["settled"])
        self.assertFalse(
            (await self.repository.record_receipt(self.token_id, record, outcome="success"))[
                "settled"
            ]
        )
        restarted = VidsOutcomeRepository(Database(self.path))
        self.assertFalse(
            (await restarted.record_receipt(self.token_id, record, outcome="success"))["settled"]
        )
        stats = await restarted.stats(self.token_id)
        self.assertEqual(
            (stats["image_count"], stats["success_count"], stats["error_count"]), (1, 1, 0)
        )
        logs = await self.db.get_logs(include_payload=True)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["operation"], "vids_generate_image")
        self.assertEqual(logs[0]["status_code"], 200)
        self.assertNotIn("private", json.dumps(logs))
        self.assertEqual(json.loads(logs[0]["response_body"])["status"], "completed")
        flow = await self.db.get_token_stats(self.token_id)
        self.assertEqual(
            (flow.image_count, flow.error_count, flow.consecutive_error_count), (0, 0, 0)
        )

    async def test_pending_and_download_recovery_updates_same_log_without_strikes(self):
        for status in ("generating", "received", "download_failed"):
            result = await self.repository.record_receipt(
                self.token_id, self.receipt(status=status)
            )
            self.assertFalse(result["settled"])
        stats = await self.repository.stats(self.token_id)
        self.assertEqual((stats["success_count"], stats["error_count"]), (0, 0))
        await self.repository.record_receipt(self.token_id, self.receipt(), outcome="success")
        self.assertEqual(len(await self.db.get_logs()), 1)
        self.assertEqual((await self.db.get_logs())[0]["status_text"], "completed")
        # A late caller holding an older observation cannot overwrite a final log.
        await self.repository.record_receipt(self.token_id, self.receipt(status="download_failed"))
        self.assertEqual((await self.db.get_logs())[0]["status_text"], "completed")

    async def test_unknown_acceptance_and_upload_never_add_account_strikes(self):
        await self.repository.record_receipt(
            self.token_id, self.receipt(status="unknown", error="acceptance_unknown")
        )
        result = await self.repository.record_receipt(
            self.token_id,
            self.receipt("upload", operation="insert", status="failed"),
            outcome="failure",
        )
        self.assertFalse(result["inserted"])
        self.assertEqual(len(await self.db.get_logs()), 1)
        self.assertEqual((await self.repository.stats(self.token_id))["error_count"], 0)
        self.assertEqual(await self.repository.public_states(), {})
        for status in ("unknown", "download_failed", "received"):
            with self.assertRaises(ValueError):
                await self.repository.record_receipt(
                    self.token_id, self.receipt(status=status), outcome="failure"
                )

    async def test_neutral_final_log_latency_and_status_stay_stable_on_replay(self):
        with patch("src.core.storage.vids_outcomes.time.time", return_value=1000):
            await self.repository.record_receipt(self.token_id, self.receipt(status="unknown"))
        initial = (await self.db.get_logs(include_payload=True))[0]
        with patch("src.core.storage.vids_outcomes.time.time", return_value=2000):
            await self.repository.record_receipt(self.token_id, self.receipt(status="unknown"))
            await self.repository.record_receipt(self.token_id, self.receipt(status="generating"))
        final = (await self.db.get_logs(include_payload=True))[0]
        self.assertEqual(
            (final["status_text"], final["duration"]), ("unknown", initial["duration"])
        )

    async def test_failure_threshold_cooldown_expiry_success_and_manual_switch_independent(self):
        failure = {"status": "failed", "error": "upstream_http_400"}
        with patch("src.core.storage.vids_outcomes.time.time", return_value=1000):
            await self.repository.record_receipt(
                self.token_id,
                self.receipt("f1", **failure),
                outcome="failure",
                consecutive_failure_threshold=2,
            )
            await self.repository.record_receipt(
                self.token_id,
                self.receipt("f2", **failure),
                outcome="failure",
                consecutive_failure_threshold=2,
                failure_cooldown_seconds=40,
            )
            self.assertEqual(
                (await self.repository.stats(self.token_id))["consecutive_error_count"], 2
            )
            states = await self.repository.public_states()
            self.assertEqual(states[self.token_id]["status"], "cooldown")
            self.assertIsNotNone(await self.repository.availability(self.token_id))
        await self.db.update_token(self.token_id, is_active=False, ban_reason="manual")
        with patch("src.core.storage.vids_outcomes.time.time", return_value=1041):
            self.assertEqual(await self.repository.public_states(), {})
            self.assertEqual(
                (await self.repository.stats(self.token_id))["consecutive_error_count"], 0
            )
            await self.repository.record_receipt(
                self.token_id, self.receipt("f3", **failure), outcome="failure"
            )
            await self.repository.record_receipt(
                self.token_id, self.receipt("success"), outcome="success"
            )
            self.assertEqual(
                (await self.repository.stats(self.token_id))["consecutive_error_count"], 0
            )
        self.assertFalse((await self.db.get_token(self.token_id)).is_active)
        self.assertEqual((await self.db.get_token(self.token_id)).ban_reason, "manual")

    async def test_rules_protect_vids_only_preserve_stronger_state_and_clear(self):
        for key, action in (("disable", "DISABLE"), ("limit", "LIMIT"), ("abnormal", "DEACTIVE")):
            await self.repository.record_receipt(
                self.token_id,
                self.receipt(key, status="failed"),
                outcome="failure",
                decision={"keyword": "Synthetic policy", "action": action, "seconds": 60},
            )
        state = (await self.repository.public_states())[self.token_id]
        self.assertEqual(state["action"], "DISABLE")
        self.assertTrue((await self.db.get_token(self.token_id)).is_active)
        self.assertEqual((await self.db.get_token_stats(self.token_id)).consecutive_error_count, 0)
        await self.db.update_token(self.token_id, is_active=False, ban_reason="manual")
        await self.repository.clear(self.token_id)
        self.assertEqual(await self.repository.public_states(), {})
        self.assertEqual((await self.repository.stats(self.token_id))["consecutive_error_count"], 0)
        self.assertFalse((await self.db.get_token(self.token_id)).is_active)

    async def test_late_success_recounts_and_releases_threshold_cooldown(self):
        with patch("src.core.storage.vids_outcomes.time.time", return_value=1000):
            for event_at in (800, 950):
                await self.repository.record_receipt(
                    self.token_id,
                    self.receipt(f"failure-{event_at}", status="failed", updated_at=event_at),
                    outcome="failure",
                    consecutive_failure_threshold=2,
                    failure_cooldown_seconds=300,
                )
            self.assertEqual(
                (await self.repository.stats(self.token_id))["consecutive_error_count"], 2
            )
            self.assertEqual(
                (await self.repository.public_states())[self.token_id]["action"], "LIMIT"
            )
            # The receipt arrived last, but its success occurred between the
            # failures: only the failure at 950 is consecutive now.
            await self.repository.record_receipt(
                self.token_id,
                self.receipt("middle-success", updated_at=900),
                outcome="success",
                consecutive_failure_threshold=2,
            )
            stats = await self.repository.stats(self.token_id)
            self.assertEqual(
                (stats["success_count"], stats["error_count"], stats["consecutive_error_count"]),
                (1, 2, 1),
            )
            self.assertEqual(await self.repository.public_states(), {})

    async def test_clear_ignores_late_rule_failure_but_keeps_new_failure_protection(self):
        decision = {"keyword": "Synthetic policy", "action": "DISABLE", "seconds": 0}
        with patch("src.core.storage.vids_outcomes.time.time", return_value=1000):
            await self.repository.clear(self.token_id)
        with patch("src.core.storage.vids_outcomes.time.time", return_value=1100):
            await self.repository.record_receipt(
                self.token_id,
                self.receipt("late-before-clear", status="failed", updated_at=950),
                outcome="failure",
                decision=decision,
            )
            self.assertEqual(await self.repository.public_states(), {})
            stats = await self.repository.stats(self.token_id)
            self.assertEqual((stats["error_count"], stats["consecutive_error_count"]), (1, 0))
            # Clearing protection creates a boundary, not a permanent bypass.
            await self.repository.record_receipt(
                self.token_id,
                self.receipt("new-after-clear", status="failed", updated_at=1050),
                outcome="failure",
                decision=decision,
            )
            self.assertEqual(
                (await self.repository.public_states())[self.token_id]["action"], "DISABLE"
            )
            stats = await self.repository.stats(self.token_id)
            self.assertEqual((stats["error_count"], stats["consecutive_error_count"]), (2, 1))
            self.assertTrue((await self.db.get_token(self.token_id)).is_active)

    async def test_out_of_order_failure_streak_cools_from_latest_failure(self):
        with patch("src.core.storage.vids_outcomes.time.time", return_value=2000):
            for event_at in (1950, 1000):
                await self.repository.record_receipt(
                    self.token_id,
                    self.receipt(f"failure-{event_at}", status="failed", updated_at=event_at),
                    outcome="failure",
                    consecutive_failure_threshold=2,
                    failure_cooldown_seconds=300,
                )
            self.assertEqual(
                (await self.repository.stats(self.token_id))["consecutive_error_count"], 2
            )
            state = (await self.repository.public_states())[self.token_id]
            self.assertEqual(state["until"], "1970-01-01T00:37:30+00:00")

    async def test_late_success_preserves_rule_cooldown_covered_by_threshold(self):
        with patch("src.core.storage.vids_outcomes.time.time", return_value=1000):
            for key, event_at, decision in (
                ("early-failure", 800, None),
                (
                    "rule-failure",
                    940,
                    {"keyword": "Synthetic policy", "action": "LIMIT", "seconds": 100},
                ),
                ("latest-failure", 950, None),
            ):
                await self.repository.record_receipt(
                    self.token_id,
                    self.receipt(key, status="failed", updated_at=event_at),
                    outcome="failure",
                    decision=decision,
                    consecutive_failure_threshold=3,
                )
            await self.repository.record_receipt(
                self.token_id,
                self.receipt("middle-success", updated_at=900),
                outcome="success",
                consecutive_failure_threshold=3,
            )
            self.assertEqual(
                (await self.repository.stats(self.token_id))["consecutive_error_count"], 2
            )
            # This success splits the streak but predates the rule failure.
            # Releasing threshold protection must reveal that later rule.
            state = (await self.repository.public_states())[self.token_id]
            self.assertEqual((state["action"], state["reason"]), ("LIMIT", "Synthetic policy"))
            self.assertEqual(state["until"], "1970-01-01T00:17:20+00:00")

    async def test_expired_cooldown_keeps_failure_that_occurred_after_deadline(self):
        with patch("src.core.storage.vids_outcomes.time.time", return_value=1000):
            await self.repository.record_receipt(
                self.token_id,
                self.receipt("before-expiry", status="failed", updated_at=1000),
                outcome="failure",
                consecutive_failure_threshold=1,
                failure_cooldown_seconds=300,
            )
        # The first observation after expiry happens much later than expiry.
        # A newly delivered event in between still belongs to the new streak.
        with patch("src.core.storage.vids_outcomes.time.time", return_value=2000):
            await self.repository.record_receipt(
                self.token_id,
                self.receipt("after-expiry", status="failed", updated_at=1500),
                outcome="failure",
                consecutive_failure_threshold=2,
            )
            self.assertEqual(await self.repository.public_states(), {})
            stats = await self.repository.stats(self.token_id)
            self.assertEqual((stats["error_count"], stats["consecutive_error_count"]), (2, 1))

    async def test_late_success_reveals_shorter_rule_after_older_longer_rule(self):
        with patch("src.core.storage.vids_outcomes.time.time", return_value=1000):
            for key, event_at, seconds in (
                ("older-long-rule", 800, 600),
                ("newer-short-rule", 950, 100),
            ):
                await self.repository.record_receipt(
                    self.token_id,
                    self.receipt(key, status="failed", updated_at=event_at),
                    outcome="failure",
                    decision={"keyword": key, "action": "LIMIT", "seconds": seconds},
                )
            await self.repository.record_receipt(
                self.token_id,
                self.receipt("middle-success", updated_at=900),
                outcome="success",
            )
            state = (await self.repository.public_states())[self.token_id]
            self.assertEqual((state["action"], state["reason"]), ("LIMIT", "newer-short-rule"))
            self.assertEqual(state["until"], "1970-01-01T00:17:30+00:00")

    async def test_two_database_instances_race_once(self):
        other = VidsOutcomeRepository(Database(self.path))
        await asyncio.gather(self.repository.ensure_schema(), other.ensure_schema())
        results = await asyncio.gather(
            *[
                repository.record_receipt(self.token_id, self.receipt(), outcome="success")
                for repository in (self.repository, other, self.repository, other)
            ]
        )
        self.assertEqual(sum(result["settled"] for result in results), 1)
        self.assertEqual(sum(result["inserted"] for result in results), 1)
        self.assertEqual((await self.repository.stats(self.token_id))["success_count"], 1)
        self.assertEqual(len(await self.db.get_logs()), 1)

    async def test_rollback_does_not_leave_settled_receipt_or_log(self):
        await self.repository.ensure_schema()
        with patch.object(
            self.repository, "_settle", side_effect=RuntimeError("Synthetic write failure")
        ):
            with self.assertRaises(RuntimeError):
                await self.repository.record_receipt(
                    self.token_id, self.receipt(), outcome="success"
                )
        self.assertEqual(await self.db.get_logs(), [])
        async with self.db._connect() as connection:
            self.assertEqual(
                (
                    await (
                        await connection.execute("SELECT COUNT(*) FROM vids_receipt_outcomes")
                    ).fetchone()
                )[0],
                0,
            )
        self.assertTrue(
            (
                await self.repository.record_receipt(
                    self.token_id, self.receipt(), outcome="success"
                )
            )["settled"]
        )

    async def test_cleared_logs_and_deleted_accounts_never_recount_receipt(self):
        record = self.receipt()
        await self.repository.record_receipt(self.token_id, record, outcome="success")
        await self.db.clear_all_logs()
        self.assertFalse(
            (await self.repository.record_receipt(self.token_id, record, outcome="success"))[
                "settled"
            ]
        )
        self.assertEqual(await self.db.get_logs(), [])
        self.assertEqual((await self.repository.stats(self.token_id))["success_count"], 1)
        await self.db.delete_token(self.token_id)
        replacement = await self.db.add_token(Token(st="replacement", email="new@example.invalid"))
        with self.assertRaisesRegex(ValueError, "affinity"):
            await self.repository.record_receipt(replacement, record, outcome="success")
        self.assertEqual((await self.repository.stats(replacement))["success_count"], 0)

    async def test_daily_stats_reset_without_affecting_lifetime_counts(self):
        with patch.object(self.db, "_current_stats_date", return_value="2026-11-08"):
            await self.repository.record_receipt(
                self.token_id, self.receipt("yesterday"), outcome="success"
            )
        with patch.object(self.db, "_current_stats_date", return_value="2026-11-09"):
            stats = await self.repository.stats(self.token_id)
            self.assertEqual((stats["today_success_count"], stats["success_count"]), (0, 1))
            await self.repository.record_receipt(
                self.token_id, self.receipt("today", status="failed"), outcome="failure"
            )
            stats = await self.repository.stats(self.token_id)
            self.assertEqual(
                (stats["today_success_count"], stats["today_error_count"], stats["success_count"]),
                (0, 1, 1),
            )

    async def test_input_validation_and_affinity_never_expose_payload_errors(self):
        await self.repository.record_receipt(
            self.token_id, self.receipt(status="failed", error="secret-token=value")
        )
        log = (await self.db.get_logs(include_payload=True))[0]
        self.assertNotIn("secret-token", log["response_body"])
        other = await self.db.add_token(Token(st="other-session", email="other@example.invalid"))
        with self.assertRaisesRegex(ValueError, "affinity"):
            await self.repository.record_receipt(other, self.receipt(), outcome="success")
        for changes in ({"id": "private"}, {"kind": []}, {"status": []}):
            with self.assertRaises(ValueError):
                await self.repository.record_receipt(self.token_id, self.receipt(**changes))

    async def test_recovered_event_keeps_original_day_and_expired_cooldown(self):
        with (
            patch("src.core.storage.vids_outcomes.time.time", return_value=2000),
            patch.object(self.db, "_current_stats_date", return_value="2026-11-09"),
        ):
            await self.repository.record_receipt(
                self.token_id,
                self.receipt(status="failed", created_at=900, updated_at=1000),
                outcome="failure",
                consecutive_failure_threshold=1,
                failure_cooldown_seconds=40,
            )
            self.assertEqual(await self.repository.public_states(), {})
            stats = await self.repository.stats(self.token_id)
            self.assertEqual(stats["error_count"], 1)
            self.assertEqual(stats["last_error_at"], 1000)
            self.assertEqual(stats["today_error_count"], 0)
            log = (await self.db.get_logs())[0]
            self.assertEqual(log["duration"], 100)


if __name__ == "__main__":
    unittest.main()
