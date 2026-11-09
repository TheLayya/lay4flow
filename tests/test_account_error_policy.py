"""Literal account rules persist their protection without using a live provider."""

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, Header, HTTPException

from src.api.admin_routes.settings import create_settings_router
from src.core.config import config
from src.core.database import Database
from src.core.models import Token
from src.services.account_error_policy import (
    DEFAULT_RULES,
    AccountErrorPolicy,
    PolicyValidationError,
    parse_rules,
)


class AccountRuleParserTests(unittest.TestCase):
    def test_literal_rules_actions_durations_comments_and_default_limit(self):
        values = parse_rules(
            "# private admin comment\n"
            "model (x).*|LIMIT|0s\n"
            "wrong login|deactive\n"
            "busy|RETRY\n"
            "banned|DISABLE\n"
            "wait|DELAY|2m\n"
            "limit|LIMIT"
        )
        self.assertEqual(
            [rule.action for rule in values],
            ["LIMIT", "DEACTIVE", "RETRY", "DISABLE", "DELAY", "LIMIT"],
        )
        self.assertEqual([rule.seconds for rule in values], [0, 0, 0, 0, 120, 60])
        self.assertEqual(values[0].keyword, "model (x).*")

    def test_bad_rule_reports_line_without_echoing_input(self):
        values = (
            "secret-cookie-value|UNKNOWN",
            "secret-cookie-value|DELAY",
            "secret-cookie-value|DELAY|0s",
            "secret-cookie-value|DELAY|301s",
            "secret-cookie-value|LIMIT|-3s",
            "secret-cookie-value|LIMIT|8d",
            "secret-cookie-value|LIMIT|169h",
            "secret-cookie-value|DISABLE|1s",
            "|RETRY",
            "secret-cookie-value|RETRY|1s|extra",
        )
        for text in values:
            with self.subTest(text=text), self.assertRaises(PolicyValidationError) as failure:
                parse_rules("\n" + text)
            self.assertIn("2", str(failure.exception))
            self.assertNotIn("secret-cookie-value", str(failure.exception))

    def test_unpaired_unicode_and_nontext_rules_are_safe_validation_failures(self):
        for value in ("\ud800|RETRY", False, None):
            with self.subTest(value=repr(value)), self.assertRaises(PolicyValidationError):
                parse_rules(value)


class AccountErrorPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.enterContext(redirect_stdout(io.StringIO()))
        self.db = Database(str(Path(directory.name) / "test.db"))
        await self.db.init_db()
        await self.db.init_config_from_toml({"admin": {"error_ban_threshold": 3}})
        self.token_id = await self.db.add_token(
            Token(st="synthetic-session", email="account@example.invalid", is_active=True)
        )
        self.policy = AccountErrorPolicy(self.db)

    async def test_default_quota_and_custom_first_match_override_are_literal(self):
        defaults = await self.policy.get_config()
        self.assertEqual(defaults["default_rules"], DEFAULT_RULES)
        self.assertEqual(
            (await self.policy.match("prefix PUBLIC_ERROR_USER_QUOTA_REACHED"))["seconds"], 86400
        )
        await self.policy.save_config({"rules": "quota_reached|RETRY\nreached|DISABLE"})
        self.assertEqual(
            (await self.policy.match("PUBLIC_ERROR_USER_QUOTA_REACHED"))["action"], "RETRY"
        )
        await self.policy.save_config({"rules": "model.*|DISABLE"})
        self.assertIsNone(await self.policy.match("model-anything"))
        self.assertEqual((await self.policy.match("MODEL.* is unavailable"))["action"], "DISABLE")

    async def test_config_persists_restart_and_invalid_update_leaves_it_unchanged(self):
        saved = await self.policy.save_config(
            {
                "rules": "custom|DELAY|4s",
                "consecutive_failure_threshold": 5,
                "max_account_attempts": 4,
            }
        )
        self.assertEqual(await AccountErrorPolicy(self.db).get_config(), saved)
        for update in (
            {"rules": "private-value|WHAT"},
            {"max_account_attempts": 0},
            {"max_account_attempts": True},
            {"failure_cooldown_seconds": 86401},
            {"consecutive_failure_threshold": 0},
            {"unknown": 1},
        ):
            with self.subTest(update=list(update)), self.assertRaises(PolicyValidationError):
                await self.policy.save_config(update)
            self.assertEqual(await self.policy.get_config(), saved)

    async def test_legacy_error_entrypoint_uses_current_cooldown_without_disabling_account(self):
        from src.services.token_manager import TokenManager

        await self.db.update_admin_config(error_ban_threshold=1)
        await self.policy.save_config(
            {"rules": "", "consecutive_failure_threshold": 2, "failure_cooldown_seconds": 40}
        )
        manager = TokenManager(self.db, object())
        await manager.record_error(self.token_id)
        self.assertIsNone(await self.policy.availability(self.token_id))
        await manager.record_error(self.token_id)
        self.assertIsNotNone(await self.policy.availability(self.token_id))
        self.assertTrue((await self.db.get_token(self.token_id)).is_active)
        self.assertEqual((await self.db.get_token_stats(self.token_id)).consecutive_error_count, 2)

    async def test_quota_cooldown_is_persistent_preserves_enable_and_expires_precisely(self):
        with patch("src.services.account_error_policy.time.time", return_value=1000):
            rule = await self.policy.record_failure(
                self.token_id, "PUBLIC_ERROR_USER_QUOTA_REACHED"
            )
            self.assertEqual(rule["action"], "LIMIT")
            state = (await AccountErrorPolicy(self.db).public_states())[self.token_id]
            self.assertEqual(state["action"], "LIMIT")
            self.assertEqual(state["status"], "cooldown")
            self.assertIsNotNone(await self.policy.availability(self.token_id))
        self.assertTrue((await self.db.get_token(self.token_id)).is_active)
        self.assertEqual((await self.db.get_token_stats(self.token_id)).consecutive_error_count, 1)
        with patch("src.services.account_error_policy.time.time", return_value=87400):
            self.assertIsNone(await self.policy.availability(self.token_id))
        self.assertEqual((await self.db.get_token_stats(self.token_id)).consecutive_error_count, 0)

    async def test_limit_zero_excludes_only_the_current_request(self):
        result = await self.policy.record_failure(self.token_id, "PUBLIC_ERROR_MODEL_ACCESS_DENIED")
        self.assertEqual((result["action"], result["seconds"]), ("LIMIT", 0))
        self.assertEqual(await self.policy.public_states(), {})
        self.assertTrue((await self.db.get_token(self.token_id)).is_active)

    async def test_explicit_retry_and_delay_do_not_mark_the_account(self):
        await self.policy.save_config({"rules": "special retry|RETRY\nspecial wait|DELAY|3s"})
        self.assertEqual(
            (await self.policy.record_failure(self.token_id, "special retry"))["action"], "RETRY"
        )
        delayed = await self.policy.record_failure(self.token_id, "special wait")
        self.assertEqual((delayed["action"], delayed["seconds"]), ("DELAY", 3))
        self.assertEqual(await self.policy.public_states(), {})

    async def test_abnormal_protection_is_separate_from_user_enable_and_not_downgraded(self):
        await self.policy.save_config({"rules": "login_required|DEACTIVE\n" + DEFAULT_RULES})
        await self.policy.record_failure(self.token_id, "login_required")
        await self.policy.record_failure(self.token_id, "PUBLIC_ERROR_USER_QUOTA_REACHED")
        state = (await self.policy.public_states())[self.token_id]
        self.assertEqual(state["status"], "abnormal")
        self.assertIsNone(state["until"])
        self.assertTrue((await self.db.get_token(self.token_id)).is_active)
        await self.policy.clear(self.token_id)
        self.assertIsNone(await self.policy.availability(self.token_id))

    async def test_removing_or_emptying_rules_really_removes_default_matches(self):
        await self.policy.save_config({"rules": "RESOURCE_EXHAUSTED|LIMIT|2s"})
        self.assertIsNone(await self.policy.match("PUBLIC_ERROR_USER_QUOTA_REACHED"))
        await self.policy.save_config({"rules": ""})
        self.assertIsNone(await self.policy.match("RESOURCE_EXHAUSTED"))
        self.assertEqual((await AccountErrorPolicy(self.db).get_config())["rules"], "")

    async def test_disable_stays_disabled_after_clearing_and_preserves_manual_reason(self):
        await self.policy.save_config({"rules": "account ban|DISABLE"})
        await self.policy.record_failure(self.token_id, "account ban")
        token = await self.db.get_token(self.token_id)
        self.assertFalse(token.is_active)
        self.assertEqual(token.ban_reason, "rule_disabled")
        await self.policy.clear(self.token_id)
        self.assertFalse((await self.db.get_token(self.token_id)).is_active)
        await self.db.update_token(self.token_id, ban_reason="manual")
        await self.policy.record_failure(self.token_id, "account ban")
        self.assertEqual((await self.db.get_token(self.token_id)).ban_reason, "manual")

    async def test_unclassified_consecutive_errors_trip_recoverable_protection(self):
        await self.policy.save_config(
            {"consecutive_failure_threshold": 2, "failure_cooldown_seconds": 20}
        )
        with patch("src.services.account_error_policy.time.time", return_value=100):
            self.assertIsNone(await self.policy.record_failure(self.token_id, "private-error-one"))
            result = await self.policy.record_failure(self.token_id, "private-error-two")
            self.assertEqual((result["action"], result["seconds"]), ("LIMIT", 20))
            state = (await self.policy.public_states())[self.token_id]
            self.assertNotIn("private-error", str(state))
        self.assertEqual((await self.db.get_token_stats(self.token_id)).error_count, 2)
        with patch("src.services.account_error_policy.time.time", return_value=121):
            self.assertIsNone(
                await self.policy.record_failure(self.token_id, "private-error-three")
            )
        stats = await self.db.get_token_stats(self.token_id)
        self.assertEqual((stats.error_count, stats.consecutive_error_count), (3, 1))

    async def test_overlapping_limits_keep_longest_until_and_no_error_response_is_stored(self):
        await self.policy.save_config({"rules": "long|LIMIT|2m\nshort|LIMIT|1s"})
        with patch("src.services.account_error_policy.time.time", return_value=100):
            await self.policy.record_failure(self.token_id, "long private-password")
        with patch("src.services.account_error_policy.time.time", return_value=101):
            await self.policy.record_failure(self.token_id, "short private-cookie")
            state = (await self.policy.public_states())[self.token_id]
        self.assertEqual(state["reason"], "long")
        self.assertEqual(state["until"], "1970-01-01T00:03:40+00:00")
        self.assertNotIn("private", str(state))

    async def test_account_deletion_cascades_protection_without_changing_other_accounts(self):
        await self.policy.record_failure(self.token_id, "login_required")
        await self.db.delete_token(self.token_id)
        self.assertEqual(await self.policy.public_states(), {})
        self.assertIsNone(await self.policy.record_failure(self.token_id, "login_required"))

    async def test_admin_api_validation_persistence_and_auth_without_generation_mutation(self):
        await self.db.update_generation_config(max_retries=3)

        async def verify(authorization: str | None = Header(default=None)):
            if authorization != "Bearer synthetic-admin":
                raise HTTPException(401, "Unauthorized")
            return "synthetic-admin"

        router, _ = create_settings_router(
            lambda: self.db, lambda: config, AsyncMock(), verify, AsyncMock()
        )
        app = FastAPI()
        app.include_router(router)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://synthetic.invalid"
        ) as client:
            self.assertEqual((await client.get("/api/account-error-policy")).status_code, 401)
            client.headers["Authorization"] = "Bearer synthetic-admin"
            initial = (await client.get("/api/account-error-policy")).json()["config"]
            changed = await client.post(
                "/api/account-error-policy", json={"rules": "synthetic|LIMIT|2h"}
            )
            self.assertEqual(changed.status_code, 200)
            self.assertEqual(changed.json()["config"]["rules"], "synthetic|LIMIT|2h")
            rejected = await client.post(
                "/api/account-error-policy", json={"rules": "private-cookie|BROKEN"}
            )
            self.assertEqual(rejected.status_code, 400)
            self.assertNotIn("private-cookie", rejected.text)
            self.assertEqual(
                (await client.post("/api/account-error-policy", content="bad-json")).status_code,
                400,
            )
            self.assertEqual(
                (await client.post("/api/account-error-policy", json=[])).status_code, 400
            )
            saved = (await client.get("/api/account-error-policy")).json()["config"]
            self.assertEqual(saved["max_account_attempts"], initial["max_account_attempts"])
            self.assertEqual(saved["rules"], "synthetic|LIMIT|2h")
            self.assertEqual((await self.db.get_generation_config()).max_retries, 3)
            legacy = await client.post("/api/admin/config", json={"error_ban_threshold": 6})
            self.assertEqual(legacy.status_code, 200)
            self.assertEqual((await self.policy.get_config())["consecutive_failure_threshold"], 6)
            legacy_read = (await client.get("/api/admin/config")).json()
            self.assertEqual(legacy_read["error_ban_threshold"], 6)
            self.assertEqual((await self.db.get_admin_config()).error_ban_threshold, 3)
            await self.policy.record_failure(self.token_id, "synthetic")
            self.assertIsNotNone(await self.policy.availability(self.token_id))
            cleared = await client.post(
                "/api/account-error-policy/clear", json={"token_id": self.token_id}
            )
            self.assertEqual(cleared.status_code, 200)
            self.assertIsNone(await self.policy.availability(self.token_id))
