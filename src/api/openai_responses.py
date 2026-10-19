"""Minimal OpenAI Responses compatibility for Flow media generation."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict

from ..core.auth import verify_openai_api_key
from ..core.models import ChatCompletionRequest, ChatMessage
from .openai_common import OpenAIRoute, safe_error


class ResponsesRequest(BaseModel):
    model: str
    input: str | list[dict[str, Any]]
    instructions: str | None = None
    stream: bool = False
    tools: list[dict[str, Any]] | None = None
    max_output_tokens: int | None = None
    temperature: float | None = None

    model_config = ConfigDict(
        extra="allow",
        json_schema_extra={"additionalProperties": False},
    )


def _unsupported_fields(request: ResponsesRequest) -> list[str]:
    fields = list(request.model_extra or {})
    if request.tools is not None:
        fields.append("tools")
    for name in (
        "audio",
        "background",
        "conversation",
        "previous_response_id",
        "truncation",
        "max_output_tokens",
        "temperature",
    ):
        if getattr(request, name, None) is not None:
            fields.append(name)
    return fields


def _content_to_chat(content: Any) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise HTTPException(
            status_code=400, detail="Responses input content must be text or a list"
        )
    result = []
    for part in content:
        if not isinstance(part, dict):
            raise HTTPException(status_code=400, detail="Responses input content item is invalid")
        part_type = part.get("type")
        if part_type in {"input_text", "text"}:
            text = part.get("text")
            if not isinstance(text, str):
                raise HTTPException(status_code=400, detail="input_text.text must be a string")
            result.append({"type": "text", "text": text})
        elif part_type in {"input_image", "image_url"}:
            image_value = part.get("image_url")
            image_url = (
                image_value if isinstance(image_value, str) else (image_value or {}).get("url")
            )
            if not isinstance(image_url, str) or not image_url:
                raise HTTPException(status_code=400, detail="input_image.image_url is required")
            result.append({"type": "image_url", "image_url": {"url": image_url}})
        else:
            raise HTTPException(
                status_code=400, detail=f"Unsupported Responses input type: {part_type}"
            )
    return result


def _to_chat_request(request: ResponsesRequest) -> ChatCompletionRequest:
    messages: list[ChatMessage] = []
    if request.instructions:
        messages.append(ChatMessage(role="system", content=request.instructions))
    if isinstance(request.input, str):
        messages.append(ChatMessage(role="user", content=request.input))
    else:
        for item in request.input:
            if not isinstance(item, dict):
                raise HTTPException(status_code=400, detail="Responses input item is invalid")
            if item.get("type") in {"input_text", "input_image", "text", "image_url"}:
                messages.append(ChatMessage(role="user", content=_content_to_chat([item])))
                continue
            role = item.get("role", "user")
            if role not in {"user", "system", "developer"}:
                raise HTTPException(status_code=400, detail="Unsupported Responses input role")
            messages.append(
                ChatMessage(role=role, content=_content_to_chat(item.get("content", "")))
            )
    if not messages or not any(message.content for message in messages):
        raise HTTPException(status_code=400, detail="Responses input cannot be empty")
    return ChatCompletionRequest(model=request.model, messages=messages, stream=request.stream)


def _response_text(payload: dict[str, Any]) -> str:
    if "result" in payload:
        return str(payload["result"] or "")
    choices = payload.get("choices") or []
    if not choices:
        return ""
    return str((choices[0].get("message") or {}).get("content") or "")


def _media_output_item(media: dict[str, Any], item_id: str | None = None) -> dict[str, Any] | None:
    media_type = media.get("type")
    url = media.get("url")
    if media_type not in {"image", "video"} or not isinstance(url, str) or not url:
        return None
    output_type = "image_generation_call" if media_type == "image" else "video_generation_call"
    item: dict[str, Any] = {
        "id": item_id or ("ig_" if media_type == "image" else "vg_") + uuid.uuid4().hex,
        "type": output_type,
        "status": "completed",
        "result": url,
    }
    if media_type == "video" and media.get("media_id"):
        item["media_id"] = media["media_id"]
    return item


def _build_response(payload: dict[str, Any], model: str) -> dict[str, Any]:
    if isinstance(payload.get("error"), dict):
        return payload
    text = _response_text(payload)
    media = payload.get("media") if isinstance(payload.get("media"), dict) else None
    media_item = _media_output_item(media) if media else None
    response_id = "resp_" + uuid.uuid4().hex
    message_id = "msg_" + uuid.uuid4().hex
    response = {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": model,
        "output": [],
        "output_text": text,
    }
    if text:
        response["output"].append(
            {
                "id": message_id,
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        )
    if media_item:
        response["output"].append(media_item)
    if media:
        response["media"] = media
    return response


def _error_response(payload: dict[str, Any]) -> JSONResponse:
    error = payload.get("error") or {}
    return JSONResponse(
        status_code=int(error.get("status_code") or 500),
        content={
            "error": {
                "message": safe_error(error.get("message", "Generation failed")),
                "type": "server_error",
                "code": error.get("code", "generation_failed"),
            }
        },
    )


def create_responses_router() -> APIRouter:
    router = APIRouter(route_class=OpenAIRoute)

    @router.post("/v1/responses")
    async def create_response(
        request: ResponsesRequest,
        raw_request: Request,
        api_key: str = Depends(verify_openai_api_key),
    ):
        unsupported = _unsupported_fields(request)
        if unsupported:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported Responses fields: {', '.join(unsupported)}",
            )
        from . import routes

        chat_request = _to_chat_request(request)
        normalized = await routes._normalize_openai_request(chat_request)
        base_url = routes._get_request_base_url(raw_request)
        if request.stream:
            return StreamingResponse(
                _stream_response(routes, normalized, request.model, base_url),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        payload = routes._parse_handler_result(
            await routes._collect_non_stream_result(
                normalized.model,
                normalized.prompt,
                normalized.images,
                base_url_override=base_url,
                video_media_id=normalized.video_media_id,
                video_bytes=normalized.video_bytes,
                video_mime_type=normalized.video_mime_type,
                generation_options=normalized.generation_options,
                response_model=normalized.public_model,
            )
        )
        if "error" in payload:
            return _error_response(payload)
        return JSONResponse(content=_build_response(payload, request.model))

    return router


async def _stream_response(routes, normalized, model: str, base_url: str | None):
    response_id = "resp_" + uuid.uuid4().hex
    created = int(time.time())
    final_text = ""
    final_media_item = None
    final_media_item_id = None
    final_media = None
    emitted_media_key = None
    yield f"event: response.created\ndata: {json.dumps({'type': 'response.created', 'response': {'id': response_id, 'object': 'response', 'created_at': created, 'status': 'in_progress', 'model': model, 'output': []}}, ensure_ascii=False)}\n\n"
    try:
        async for chunk in routes._iterate_openai_stream(normalized, base_url):
            if chunk.strip() == "data: [DONE]":
                continue
            payload = routes._parse_handler_result(
                chunk[6:].strip() if chunk.startswith("data: ") else chunk
            )
            if "error" in payload:
                error = payload["error"]
                failed = {
                    "type": "response.failed",
                    "response": {
                        "id": response_id,
                        "object": "response",
                        "created_at": created,
                        "status": "failed",
                        "model": model,
                        "output": [],
                        "error": error,
                    },
                }
                yield f"event: response.failed\ndata: {json.dumps(failed, ensure_ascii=False)}\n\n"
                yield f"event: error\ndata: {json.dumps({'type': 'error', 'error': error}, ensure_ascii=False)}\n\n"
                return
            text = routes._extract_openai_message_content(payload)
            if text:
                final_text += text
                event = {
                    "type": "response.output_text.delta",
                    "item_id": response_id,
                    "delta": text,
                }
                yield f"event: response.output_text.delta\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            media = payload.get("media")
            if isinstance(media, dict):
                final_media = media
                media_item_id = final_media_item_id or "ig_" + uuid.uuid4().hex
                media_item = _media_output_item(media, media_item_id)
                media_key = media.get("media_id") or media.get("url") if media_item else None
                final_media_item = media_item or final_media_item
                final_media_item_id = media_item_id if media_item else final_media_item_id
                if media_item and media_key != emitted_media_key:
                    emitted_media_key = media_key
                    event = {
                        "type": "response.output_item.added",
                        "item_id": media_item["id"],
                        "item": media_item,
                    }
                    yield f"event: response.output_item.added\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
    except Exception:
        error = {"message": "Internal server error", "code": "internal_error"}
        failed = {
            "type": "response.failed",
            "response": {
                "id": response_id,
                "object": "response",
                "created_at": created,
                "status": "failed",
                "model": model,
                "output": [],
                "error": error,
            },
        }
        yield f"event: response.failed\ndata: {json.dumps(failed, ensure_ascii=False)}\n\n"
        yield f"event: error\ndata: {json.dumps({'type': 'error', 'error': error}, ensure_ascii=False)}\n\n"
        return
    if final_text:
        done_event = {
            "type": "response.output_text.done",
            "item_id": response_id,
            "text": final_text,
        }
        yield f"event: response.output_text.done\ndata: {json.dumps(done_event, ensure_ascii=False)}\n\n"
    completed_output = []
    if final_text:
        completed_output.append(
            {
                "id": "msg_" + uuid.uuid4().hex,
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": final_text, "annotations": []}],
            }
        )
    if final_media_item:
        completed_output.append(final_media_item)
    completed = {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "object": "response",
            "created_at": created,
            "status": "completed",
            "model": model,
            "output": completed_output,
        },
    }
    if final_media:
        completed["response"]["media"] = final_media
    yield f"event: response.completed\ndata: {json.dumps(completed, ensure_ascii=False)}\n\n"
