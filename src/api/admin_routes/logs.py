"""Request-log routes built with explicit database and authentication dependencies."""

import json
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
