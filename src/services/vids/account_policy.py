"""Vids-specific configuration and once-only receipt outcome protection."""

import asyncio
import json
from pathlib import Path

from ...core.storage.vids_outcomes import VidsOutcomeRepository
from ..account_error_policy import PolicyValidationError, parse_rules

DEFAULT_CONFIG = {
    "keepalive_enabled": True,
    "keepalive_interval_minutes": 10,
    "repair_cooldown_seconds": 300,
    "consecutive_failure_threshold": 3,
    "failure_cooldown_seconds": 300,
    "rules": "",
}
_LIMITS = {
    "keepalive_interval_minutes": (1, 1440),
    "repair_cooldown_seconds": (30, 86400),
    "consecutive_failure_threshold": (1, 100),
    "failure_cooldown_seconds": (1, 86400),
}


class VidsAccountPolicy:
    """Vids rules never mutate Flow protection, counters, or manual enables."""

    def __init__(self, database):
        self.db = database
        self.outcomes = VidsOutcomeRepository(database)
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def _ensure_schema(self):
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            await self.outcomes.ensure_schema()
            async with self.db._connect(write=True) as db:
                await db.execute(
                    "CREATE TABLE IF NOT EXISTS vids_account_policy_config "
                    "(id INTEGER PRIMARY KEY CHECK(id=1), settings TEXT NOT NULL)"
                )
                await db.execute(
                    "INSERT OR IGNORE INTO vids_account_policy_config(id,settings) VALUES(1,?)",
                    (json.dumps(DEFAULT_CONFIG),),
                )
                await db.commit()
            self._initialized = True

    async def get_config(self):
        await self._ensure_schema()
        async with self.db._connect() as db:
            row = await (
                await db.execute("SELECT settings FROM vids_account_policy_config WHERE id=1")
            ).fetchone()
        return {**DEFAULT_CONFIG, **json.loads(row[0]), "default_rules": DEFAULT_CONFIG["rules"]}

    async def save_config(self, values):
        if not isinstance(values, dict) or set(values) - set(DEFAULT_CONFIG):
            raise PolicyValidationError("Vids 配置字段无效")
        if "keepalive_enabled" in values and type(values["keepalive_enabled"]) is not bool:
            raise PolicyValidationError("Vids 后台维护开关无效")
        if "rules" in values:
            if any(rule.action in {"RETRY", "DELAY"} for rule in parse_rules(values["rules"])):
                raise PolicyValidationError("Vids 规则仅支持 LIMIT、DEACTIVE、DISABLE")
        for field, (minimum, maximum) in _LIMITS.items():
            if field in values and (
                type(values[field]) is not int or not minimum <= values[field] <= maximum
            ):
                raise PolicyValidationError(f"{field} 需要 {minimum} 至 {maximum} 的整数")
        await self._ensure_schema()
        async with self.db._connect(write=True) as db:
            row = await (
                await db.execute("SELECT settings FROM vids_account_policy_config WHERE id=1")
            ).fetchone()
            settings = {**DEFAULT_CONFIG, **json.loads(row[0]), **values}
            await db.execute(
                "UPDATE vids_account_policy_config SET settings=? WHERE id=1",
                (json.dumps(settings, ensure_ascii=False),),
            )
            await db.commit()
        return {**settings, "default_rules": DEFAULT_CONFIG["rules"]}

    @staticmethod
    def classify(record):
        """Only a confirmed paid outcome may add a success or an account strike."""
        if record.get("operation") == "insert":
            return None
        status = record.get("status")
        if status == "completed":
            return "success"
        return "failure" if status in {"failed", "no_result", "specification_mismatch"} else None

    async def observe(self, token_id, record, *, request_summary=None, started_at=None):
        """Replays and download recovery may update a log but cannot resettle it."""
        if record.get("operation") == "insert":
            return {"inserted": False, "settled": False, "log_id": None}
        outcome = self.classify(record)
        settings = await self.get_config()
        decision = None
        if outcome == "failure" and record.get("error_operation") == "generate":
            # Match bounded structured protocol fields, never raw upstream bodies.
            fields = [record.get("error")]
            upstream = record.get("upstream_error")
            if isinstance(upstream, dict):
                fields.extend(upstream.get(key) for key in ("status", "reason"))
            text = " ".join(value[:4096] for value in fields if isinstance(value, str)).casefold()
            for rule in parse_rules(settings["rules"]):
                if rule.keyword.casefold() in text:
                    decision = rule.decision()
                    break
        return await self.outcomes.record_receipt(
            token_id,
            record,
            outcome=outcome,
            decision=decision,
            consecutive_failure_threshold=settings["consecutive_failure_threshold"],
            failure_cooldown_seconds=settings["failure_cooldown_seconds"],
            account_fault=outcome == "failure"
            and record.get("status") == "failed"
            and record.get("error_operation") == "generate"
            and (
                decision is not None
                or record.get("error")
                in {"upstream_http_401", "upstream_http_403", "upstream_http_429"}
            ),
            allow_insert=request_summary is not None
            or started_at is not None
            or record.get("outcome_tracking_version") == 1,
        )

    async def account_stats(self, token_id):
        return await self.outcomes.stats(token_id)

    async def account_stats_many(self, token_ids):
        return await self.outcomes.stats_many(token_ids)

    async def reconcile(self, token_id, receipt_root):
        """Recover durable outcomes after a crash without any upstream action.

        Runtime identity validation must complete before handing a binding to
        this method. Invalid local receipts do not hide other valid outcomes.
        """
        records = []
        for path in Path(receipt_root).glob("vids_*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if path.stem != record.get("id"):
                    continue
                # Older receipts lack terminal timestamps. Their atomic file
                # modification time is the best local outcome checkpoint.
                if "updated_at" not in record and "completed_at" not in record:
                    record["updated_at"] = path.stat().st_mtime
                created = self.outcomes._timestamp(record.get("created_at"), 0)
                records.append((created, record["id"], record))
            except (ValueError, OSError, UnicodeError, TypeError, AttributeError):
                continue
        for _, _, record in sorted(records):
            try:
                await self.observe(token_id, record)
            except (ValueError, TypeError):
                continue

    async def public_states(self):
        return await self.outcomes.public_states()

    async def availability(self, token_id):
        return await self.outcomes.availability(token_id)

    async def clear(self, token_id):
        return await self.outcomes.clear(token_id)
