"""Request-log routes built with explicit database and authentication dependencies."""

import json
import math
import re
from datetime import datetime
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException


def _truncate_text(text: Any, limit: int = 240) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return f"{value[: limit - 3]}..."


def _extract_error_summary(payload: Any) -> str:
    """从响应体里提取用户可读的错误摘要。"""
    if payload is None:
        return ""

    if isinstance(payload, str):
        raw = payload.strip()
        if not raw:
            return ""
        try:
            return _extract_error_summary(json.loads(raw))
        except Exception:
            return _truncate_text(raw)

    if isinstance(payload, dict):
        for key in ("error_summary", "error_message", "detail", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return _truncate_text(value)

        error_value = payload.get("error")
        if isinstance(error_value, dict):
            for key in ("message", "detail", "reason", "code"):
                value = error_value.get(key)
                if isinstance(value, str) and value.strip():
                    return _truncate_text(value)
        elif isinstance(error_value, str) and error_value.strip():
            return _truncate_text(error_value)

        for nested_key in ("response", "data"):
            nested = payload.get(nested_key)
            if isinstance(nested, (dict, list, str)):
                summary = _extract_error_summary(nested)
                if summary:
                    return summary

        return ""

    if isinstance(payload, list):
        for item in payload:
            summary = _extract_error_summary(item)
            if summary:
                return summary
        return ""

    return _truncate_text(payload)


def _attempt_count(value: Any) -> int | None:
    """Unknown legacy counts remain unknown; booleans are not counted attempts."""
    return value if type(value) is int and 0 <= value <= 1_000_000 else None


def _safe_attempt_text(value: Any, limit: int = 240) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    text = re.sub(r"\bcookie\s*[:=]\s*[^\r\n]*", "Cookie=[redacted]", text, flags=re.I)
    text = re.sub(r"(?:https?|socks4a?|socks5h?)://\S+", "[URL]", text, flags=re.I)
    text = re.sub(r"\bBearer\s+\S+", "Bearer [redacted]", text, flags=re.I)
    text = re.sub(
        r"\b(cookie|authorization|password|token|api[_-]?key)\s*[:=]\s*\S+",
        r"\1=[redacted]",
        text,
        flags=re.I,
    )
    # Opaque credential echoes may have no identifying label. Public Flow
    # error identifiers are safe evidence and must stay visible in the trace.
    text = re.sub(
        r"[A-Za-z0-9_+/=.-]{48,}",
        lambda match: (
            match.group(0)
            if re.fullmatch(r"PUBLIC_ERROR_[A-Z0-9_]+", match.group(0))
            else "[redacted]"
        ),
        text,
    )
    return _truncate_text(text, limit)


def _extract_attempt_history(payload: Any) -> dict | None:
    """Expose bounded structured evidence, never guess retries from log rows."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError, RecursionError):
            return None
    if not isinstance(payload, dict):
        return None
    history = payload.get("attempt_history")
    if history is None and isinstance(payload.get("performance"), dict):
        history = payload["performance"].get("attempt_history")
    if (
        not isinstance(history, dict)
        or type(history.get("version")) is not int
        or history["version"] not in {1, 2}
        or not isinstance(history.get("attempts"), list)
    ):
        return None
    result = {"version": history["version"]}
    count_fields = ["total_attempts", "retry_count", "account_count", "limit"]
    count_fields.append("accounts_limit" if history["version"] == 1 else "account_allocations")
    for name in count_fields:
        count = _attempt_count(history.get(name))
        if count is not None:
            result[name] = count
    attempts = []
    for record in history["attempts"][:100]:
        if not isinstance(record, dict):
            continue
        number = _attempt_count(record.get("attempt"))
        if not number:
            continue
        row = {"attempt": number}
        account_id = _attempt_count(record.get("account_id"))
        if account_id:
            row["account_id"] = account_id
        if isinstance(record.get("account_email"), str):
            row["account_email"] = " ".join(record["account_email"].split())[:320]
        if record.get("stage") in {"captcha", "submit", "preflight", "polling"}:
            row["stage"] = record["stage"]
        if record.get("status") in {"success", "accepted", "failed", "in_progress", "cancelled"}:
            row["status"] = record["status"]
        if record.get("next_action") in {
            "switch_account",
            "retry_same_account",
            "retry",
            "delay",
            "stop",
            "completed",
            "polling",
        }:
            row["next_action"] = record["next_action"]
        if record.get("rule_action") in {"LIMIT", "DEACTIVE", "RETRY", "DISABLE", "DELAY"}:
            row["rule_action"] = record["rule_action"]
        for name, limit in (("error", 240), ("error_code", 128), ("rule_keyword", 256)):
            if isinstance(record.get(name), str):
                row[name] = _safe_attempt_text(record[name], limit)
        started_at = record.get("started_at")
        if isinstance(started_at, str) and len(started_at) <= 40:
            try:
                datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                row["started_at"] = started_at
            except ValueError:
                pass
        for name, maximum in (
            ("duration_ms", 604_800_000),
            ("delay_seconds", 604_800),
            ("rule_seconds", 604_800),
        ):
            value = record.get(name)
            if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= maximum:
                row[name] = value
        attempts.append(row)
    result["attempts"] = attempts
    if len(history["attempts"]) > 100:
        result["truncated"] = True
    return result


def create_log_router(get_database: Callable, verify_token: Callable, summarize_error: Callable):
    """Return the router and compatible direct-call endpoints; no service globals."""
    router = APIRouter()

    @router.get("/api/logs")
    async def get_logs(limit: int = 100, token: str = Depends(verify_token)):
        """Get lightweight request logs for list view"""
        limit = max(1, min(limit, 100))
        logs = await get_database().get_logs(limit=limit, include_payload=False)

        result = []
        for log in logs:
            raw_status_code = log.get("status_code")
            try:
                status_code = int(raw_status_code) if raw_status_code is not None else None
            except (TypeError, ValueError):
                status_code = None
            result.append(
                {
                    "id": log.get("id"),
                    "token_id": log.get("token_id"),
                    "token_email": log.get("token_email"),
                    "token_username": log.get("token_username"),
                    "operation": log.get("operation"),
                    "status_code": status_code if status_code is not None else raw_status_code,
                    "duration": log.get("duration"),
                    "status_text": log.get("status_text") or "",
                    "progress": log.get("progress") or 0,
                    "attempt_count": _attempt_count(log.get("attempt_count")),
                    "retry_count": _attempt_count(log.get("retry_count")),
                    "account_count": _attempt_count(log.get("account_count")),
                    "created_at": log.get("created_at"),
                    "updated_at": log.get("updated_at"),
                    "error_summary": summarize_error(log.get("response_body_excerpt"))
                    if status_code is not None and status_code >= 400
                    else "",
                }
            )
        return result

    @router.get("/api/logs/{log_id}")
    async def get_log_detail(log_id: int, token: str = Depends(verify_token)):
        """Get single request log detail (payload loaded on demand)"""
        log = await get_database().get_log_detail(log_id)
        if not log:
            raise HTTPException(status_code=404, detail="日志不存在")

        error_summary = summarize_error(log.get("response_body"))

        return {
            "id": log.get("id"),
            "token_id": log.get("token_id"),
            "token_email": log.get("token_email"),
            "token_username": log.get("token_username"),
            "operation": log.get("operation"),
            "status_code": log.get("status_code"),
            "duration": log.get("duration"),
            "status_text": log.get("status_text") or "",
            "progress": log.get("progress") or 0,
            "attempt_count": _attempt_count(log.get("attempt_count")),
            "retry_count": _attempt_count(log.get("retry_count")),
            "account_count": _attempt_count(log.get("account_count")),
            "attempt_history": _extract_attempt_history(log.get("response_body")),
            "created_at": log.get("created_at"),
            "updated_at": log.get("updated_at"),
            "error_summary": error_summary,
            "request_body": log.get("request_body"),
            "response_body": log.get("response_body"),
        }

    @router.delete("/api/logs")
    async def clear_logs(token: str = Depends(verify_token)):
        """Clear all logs"""
        try:
            await get_database().clear_all_logs()
            return {"success": True, "message": "所有日志已清空"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    return router, get_logs, get_log_detail, clear_logs
