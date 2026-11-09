"""Persistent generation-account protection and literal, administrator-owned rules."""

import asyncio
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

DEFAULT_RULES = "\n".join(
    (
        "PUBLIC_ERROR_MODEL_ACCESS_DENIED|LIMIT|0s",
        "PUBLIC_ERROR_USER_QUOTA_REACHED|LIMIT|24h",
        "Resource has been exhausted (e.g. check quota).|LIMIT|1s",
        "PUBLIC_ERROR_USER_REQUESTS_THROTTLED|LIMIT|3s",
        "RESOURCE_EXHAUSTED|LIMIT|5s",
    )
)
DEFAULT_CONFIG = {
    "rules": DEFAULT_RULES,
    "consecutive_failure_threshold": 3,
    "failure_cooldown_seconds": 300,
    "max_account_attempts": 3,
}
ACTIONS = {"LIMIT", "DEACTIVE", "RETRY", "DISABLE", "DELAY"}
_LIMITS = {
    "consecutive_failure_threshold": (1, 100),
    "failure_cooldown_seconds": (1, 86400),
    "max_account_attempts": (1, 10),
}


class PolicyValidationError(ValueError):
    """Safe configuration failure; never echoes submitted rules or credentials."""


@dataclass(frozen=True)
class ErrorRule:
    keyword: str
    action: str
    seconds: int = 0

    def decision(self) -> dict[str, Any]:
        return {"keyword": self.keyword, "action": self.action, "seconds": self.seconds}


def parse_rules(text: str) -> list[ErrorRule]:
    """Parse literal, case-insensitive first-match rules; regular expressions are not run."""
    try:
        size = len(text.encode("utf-8")) if isinstance(text, str) else 65537
    except UnicodeError:
        raise PolicyValidationError("自定义规则包含无效文本编码") from None
    if size > 65536:
        raise PolicyValidationError("自定义规则不能超过 64 KiB")
    rules = []
    for line_number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        def invalid(detail):
            return PolicyValidationError(f"第 {line_number} 行：{detail}")

        parts = [part.strip() for part in line.split("|")]
        if len(parts) not in (2, 3):
            raise invalid("格式应为 关键字|操作 或 关键字|操作|时间")
        keyword, action = parts[:2]
        if (
            not keyword
            or len(keyword) > 256
            or any(ord(char) < 32 or ord(char) == 127 for char in keyword)
        ):
            raise invalid("关键字需要 1 至 256 个可见字符")
        action = action.upper()
        if action not in ACTIONS:
            raise invalid("操作仅支持 LIMIT、DEACTIVE、RETRY、DISABLE、DELAY")
        seconds = 60 if action == "LIMIT" else 0
        if len(parts) == 3:
            if action not in {"LIMIT", "DELAY"}:
                raise invalid("只有 LIMIT 和 DELAY 支持时间")
            match = re.fullmatch(r"(\d{1,7})([smh])", parts[2], re.I)
            if not match:
                raise invalid("时间必须使用非负整数及 s、m、h 单位")
            seconds = int(match[1]) * {"s": 1, "m": 60, "h": 3600}[match[2].lower()]
            if seconds > 7 * 86400:
                raise invalid("限速时间不能超过 7 天")
        elif action == "DELAY":
            raise invalid("DELAY 必须指定延时时间")
        if action == "DELAY" and not 1 <= seconds <= 300:
            raise invalid("DELAY 需要 1 至 300 秒")
        rules.append(ErrorRule(keyword, action, seconds))
        if len(rules) > 200:
            raise invalid("最多配置 200 条规则")
    return rules


class AccountErrorPolicy:
    """Keep account protection separate from user enable/disable and login refresh state."""

    def __init__(self, database):
        self.db = database
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def _ensure_schema(self):
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            async with self.db._connect(write=True) as db:
                await db.execute(
                    "CREATE TABLE IF NOT EXISTS account_error_policy_config "
                    "(id INTEGER PRIMARY KEY CHECK(id=1), settings TEXT NOT NULL)"
                )
                await db.execute(
                    "CREATE TABLE IF NOT EXISTS account_error_protection "
                    "(token_id INTEGER PRIMARY KEY, action TEXT NOT NULL, reason TEXT NOT NULL, "
                    "until_at REAL, created_at REAL NOT NULL, "
                    "FOREIGN KEY(token_id) REFERENCES tokens(id) ON DELETE CASCADE)"
                )
                await db.execute(
                    "INSERT OR IGNORE INTO account_error_policy_config(id,settings) VALUES(1,?)",
                    (json.dumps(DEFAULT_CONFIG),),
                )
                await db.commit()
            self._initialized = True

    async def get_config(self) -> dict[str, Any]:
        await self._ensure_schema()
        async with self.db._connect() as db:
            row = await (
                await db.execute("SELECT settings FROM account_error_policy_config WHERE id=1")
            ).fetchone()
        settings = {**DEFAULT_CONFIG, **json.loads(row[0])}
        return {**settings, "default_rules": DEFAULT_RULES}

    async def save_config(self, values: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(values, dict) or set(values) - set(DEFAULT_CONFIG):
            raise PolicyValidationError("规则配置字段无效")
        # Validate the entire update before any mutation or schema creation.
        if "rules" in values:
            parse_rules(values["rules"])
        for field, (minimum, maximum) in _LIMITS.items():
            if field in values and (
                type(values[field]) is not int or not minimum <= values[field] <= maximum
            ):
                raise PolicyValidationError(f"{field} 需要 {minimum} 至 {maximum} 的整数")
        await self._ensure_schema()
        async with self.db._connect(write=True) as db:
            row = await (
                await db.execute("SELECT settings FROM account_error_policy_config WHERE id=1")
            ).fetchone()
            settings = {**DEFAULT_CONFIG, **json.loads(row[0]), **values}
            await db.execute(
                "UPDATE account_error_policy_config SET settings=? WHERE id=1",
                (json.dumps(settings, ensure_ascii=False),),
            )
            await db.commit()
        return {**settings, "default_rules": DEFAULT_RULES}

    async def match(self, error) -> dict[str, Any] | None:
        settings = await self.get_config()
        text = str(error or "").casefold()
        for rule in parse_rules(settings["rules"]):
            if rule.keyword.casefold() in text:
                return rule.decision()
        return None

    async def record_failure(self, token_id: int, error, model=None) -> dict[str, Any] | None:
        """Record exactly one failed attempt, then apply its account rule once."""
        token = await self.db.get_token(token_id)
        if token is None:
            return None
        # Expiry starts a fresh consecutive-error window even after a restart.
        await self._expire(time.time(), token_id)
        await self.db.increment_token_stats(token_id, "error")
        decision = await self.match(error)
        if (
            decision is None
            or decision["action"] in {"RETRY", "DELAY"}
            or (decision["action"] == "LIMIT" and decision["seconds"] == 0)
        ):
            settings = await self.get_config()
            stats = await self.db.get_token_stats(token_id)
            if stats and stats.consecutive_error_count >= settings["consecutive_failure_threshold"]:
                decision = {
                    "keyword": "连续生成失败保护",
                    "action": "LIMIT",
                    "seconds": settings["failure_cooldown_seconds"],
                }
        if decision is None:
            return None
        action, seconds = decision["action"], decision["seconds"]
        if action in {"RETRY", "DELAY"} or action == "LIMIT" and seconds == 0:
            return decision
        await self._ensure_schema()
        now = time.time()
        until = now + seconds if action == "LIMIT" else None
        reason = decision["keyword"][:256]
        async with self.db._connect(write=True) as db:
            # A late parallel LIMIT must never clear a prior abnormal/disabled
            # decision; overlapping rate limits retain the longer deadline.
            row = await (
                await db.execute(
                    "SELECT action,until_at FROM account_error_protection WHERE token_id=?",
                    (token_id,),
                )
            ).fetchone()
            rank = {"LIMIT": 1, "DEACTIVE": 2, "DISABLE": 3}
            replace = (
                row is None
                or rank[action] > rank[row[0]]
                or (
                    rank[action] == rank[row[0]]
                    and (until is None or row[1] is not None and until > row[1])
                )
            )
            if replace:
                await db.execute(
                    "INSERT INTO account_error_protection(token_id,action,reason,until_at,created_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(token_id) DO UPDATE SET "
                    "action=excluded.action,reason=excluded.reason,until_at=excluded.until_at,"
                    "created_at=excluded.created_at",
                    (token_id, action, reason, until, now),
                )
            if action == "DISABLE":
                # Do not replace a user/manual disable reason with a background
                # failure that finished after their settings change.
                await db.execute(
                    "UPDATE tokens SET is_active=0,ban_reason='rule_disabled',banned_at=? "
                    "WHERE id=? AND is_active=1",
                    (datetime.fromtimestamp(now, timezone.utc), token_id),
                )
            await db.commit()
        return decision

    async def _expire(self, now, token_id=None):
        await self._ensure_schema()
        async with self.db._connect(write=True) as db:
            query = (
                "SELECT token_id FROM account_error_protection "
                "WHERE action='LIMIT' AND until_at <= ?"
            )
            arguments = [now]
            if token_id is not None:
                query += " AND token_id=?"
                arguments.append(token_id)
            rows = await (await db.execute(query, arguments)).fetchall()
            if rows:
                ids = [row[0] for row in rows]
                marks = ",".join("?" for _ in ids)
                await db.execute(
                    f"DELETE FROM account_error_protection WHERE token_id IN ({marks})", ids
                )
                await db.execute(
                    f"UPDATE token_stats SET consecutive_error_count=0 WHERE token_id IN ({marks})",
                    ids,
                )
                await db.commit()

    async def public_states(self) -> dict[int, dict[str, Any]]:
        await self._expire(time.time())
        async with self.db._connect() as db:
            rows = await (
                await db.execute(
                    "SELECT token_id,action,reason,until_at FROM account_error_protection"
                )
            ).fetchall()
        return {
            token_id: {
                "action": action,
                "status": {"LIMIT": "cooldown", "DEACTIVE": "abnormal", "DISABLE": "disabled"}[
                    action
                ],
                "until": datetime.fromtimestamp(until, timezone.utc).isoformat() if until else None,
                "reason": reason,
            }
            for token_id, action, reason, until in rows
        }

    async def availability(self, token_id: int, model=None) -> str | None:
        await self._expire(time.time(), token_id)
        async with self.db._connect() as db:
            row = await (
                await db.execute(
                    "SELECT action,reason,until_at FROM account_error_protection WHERE token_id=?",
                    (token_id,),
                )
            ).fetchone()
        if row is None:
            return None
        action, reason, until = row
        if action == "LIMIT":
            return f"账号冷却至 {datetime.fromtimestamp(until, timezone.utc).isoformat()}：{reason}"
        return f"账号{'异常' if action == 'DEACTIVE' else '已停用'}：{reason}"

    async def clear(self, token_id: int):
        """Clear automatic protection, without enabling a manually disabled account."""
        await self._ensure_schema()
        async with self.db._connect(write=True) as db:
            await db.execute("DELETE FROM account_error_protection WHERE token_id=?", (token_id,))
            await db.execute(
                "UPDATE token_stats SET consecutive_error_count=0 WHERE token_id=?", (token_id,)
            )
            await db.commit()
