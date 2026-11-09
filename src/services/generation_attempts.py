"""Request-local generation bounds and failed-account exclusions."""

import re
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone


def safe_attempt_text(value, limit=360):
    text = " ".join(str(value or "").split())
    text = re.sub(r"\bcookie\s*[:=]\s*[^\r\n]*", "Cookie=[redacted]", text, flags=re.I)
    text = re.sub(r"(?:https?|socks4a?|socks5h?)://\S+", "[URL]", text, flags=re.I)
    text = re.sub(r"\bBearer\s+\S+", "Bearer [redacted]", text, flags=re.I)
    text = re.sub(
        r"(?i)(cookie|authorization|password|api[_-]?key|token)\s*[:=]\s*\S+",
        r"\1=[redacted]",
        text,
    )
    text = re.sub(
        r"[A-Za-z0-9_+/=.-]{48,}",
        lambda match: (
            match.group(0)
            if re.fullmatch(r"PUBLIC_ERROR_[A-Z0-9_]+", match.group(0))
            else "[redacted]"
        ),
        text,
    )
    return text[:limit]


class GenerationBudgetExceeded(RuntimeError):
    pass


@dataclass
class GenerationAttempts:
    limit: int
    used: int = 0
    excluded: set[int] = field(default_factory=set)
    accounts_limit: int = 0
    history: list[dict] = field(default_factory=list)
    account_id: int | None = None
    account_email: str = ""
    log_state: dict = field(default_factory=lambda: {"id": None, "progress": 0})
    request_id: str = field(default_factory=lambda: "gen-" + uuid.uuid4().hex)
    started_at: float = field(default_factory=time.time)
    account_attempt_start: int = 0
    account_started: float = field(default_factory=time.monotonic)
    account_started_at: str = ""
    last_log: dict = field(default_factory=dict)
    account_runs: int = 0

    def select_account(self, token):
        if self.history:
            previous = self.history[-1]
            if previous.get("rule_action") == "RETRY" and previous.get("next_action") == "retry":
                previous["next_action"] = (
                    "retry_same_account" if previous["account_id"] == token.id else "switch_account"
                )
        self.account_id = token.id
        self.account_email = str(token.email)
        self.account_attempt_start = len(self.history)
        self.account_runs += 1
        self.account_started = time.monotonic()
        self.account_started_at = datetime.now(timezone.utc).isoformat()

    def take(self):
        if self.used >= self.limit:
            raise GenerationBudgetExceeded("生成尝试次数已达到配置上限")
        self.used += 1
        self.begin("captcha")

    def begin(self, stage):
        if stage != "captcha":
            self.used = max(self.used, len(self.history) + 1)
        row = {
            "attempt": len(self.history) + 1,
            "account_id": self.account_id,
            "account_email": self.account_email,
            "stage": stage,
            "status": "in_progress",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "duration_ms": 0,
            "error": "",
            "error_code": "",
            "rule_action": "",
            "rule_keyword": "",
            "delay_seconds": 0,
            "next_action": "",
            "_started": time.monotonic(),
        }
        self.history.append(row)
        return row

    def stage(self, stage):
        if self.history and self.history[-1]["status"] == "in_progress":
            self.history[-1]["stage"] = stage

    def finish(self, status, error=None, *, next_action=None, delay_seconds=None):
        row = (
            self.history[-1]
            if len(self.history) > self.account_attempt_start
            else self.begin("preflight")
        )
        if row["stage"] == "preflight":
            row["_started"] = self.account_started
            row["started_at"] = self.account_started_at or row["started_at"]
        if row["status"] not in {"in_progress", "accepted", status} and error is None:
            return
        row["status"] = status
        row["duration_ms"] = max(0, int((time.monotonic() - row["_started"]) * 1000))
        if error is not None:
            row["error"] = safe_attempt_text(error)
            row["error_code"] = safe_attempt_text(
                getattr(error, "public_error", None) or getattr(error, "reason", ""), 120
            )
        if next_action is not None:
            row["next_action"] = next_action
        if delay_seconds is not None:
            row["delay_seconds"] = delay_seconds

    def decision(self, decision, *, retry):
        if not self.history:
            return
        row = self.history[-1]
        row["rule_action"] = decision.get("action", "")
        row["rule_keyword"] = safe_attempt_text(decision.get("keyword", ""), 256)
        if row["rule_action"] in {"LIMIT", "DELAY"}:
            row["rule_seconds"] = decision.get("seconds", 0)
        row["delay_seconds"] = decision.get("seconds", 0) if row["rule_action"] == "DELAY" else 0
        row["next_action"] = (
            "stop"
            if not retry
            else "delay"
            if row["rule_action"] == "DELAY"
            else "retry"
            if row["rule_action"] == "RETRY"
            else "switch_account"
        )

    def finalize(self, status, error=None):
        """Close request history without inventing another submission attempt."""
        if self.account_id is not None and len(self.history) == self.account_attempt_start:
            self.begin("preflight")
            self.history[-1]["_started"] = self.account_started
        if not self.history:
            return
        row = self.history[-1]
        if row["status"] in {"in_progress", "accepted"} or (
            row["status"] == "success" and status in {"success", "failed", "cancelled"}
        ):
            self.finish(status, error)
        row["next_action"] = "completed" if status == "success" else "stop"

    def snapshot(self):
        rows = []
        for row in self.history:
            item = {key: value for key, value in row.items() if not key.startswith("_")}
            if item["status"] in {"in_progress", "accepted"}:
                item["duration_ms"] = max(0, int((time.monotonic() - row["_started"]) * 1000))
            rows.append(item)
        count = len(rows)
        return {
            "version": 1,
            "request_id": self.request_id,
            "total_attempts": count,
            "retry_count": max(0, count - 1),
            "account_count": len(
                {row["account_id"] for row in rows if row["account_id"] is not None}
            ),
            "limit": self.limit,
            "accounts_limit": self.accounts_limit,
            "attempts": rows,
        }


CURRENT_ATTEMPTS: ContextVar[GenerationAttempts | None] = ContextVar(
    "lay4flow_generation_attempts", default=None
)


def is_confirmed_flow_rejection(error):
    """Only trusted, rejected writes can return to configurable account retry."""
    from .flow.flow_rpc import FlowRPCError

    return (
        isinstance(error, FlowRPCError)
        and error.submission_rejected
        and error.network_step in {"flow_generate_image", "flow_generate_video"}
    )


class AccountGenerationRetry(Exception):
    def __init__(self, error, token_id, decision):
        super().__init__(str(error))
        self.error, self.token_id, self.decision = error, token_id, decision
