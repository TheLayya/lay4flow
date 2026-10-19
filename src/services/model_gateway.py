"""Explicit platform registrations for future chat models; Flow remains the default."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, AsyncIterator, Protocol


class ChatProvider(Protocol):
    async def complete(self, request: dict[str, Any]) -> dict[str, Any]: ...

    def stream(self, request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]: ...


@dataclass(frozen=True)
class ModelDescriptor:
    model: str
    provider: ChatProvider
    project_id: str
    owned_by: str
    capabilities: dict[str, Any]

    def as_api_model(self) -> dict[str, Any]:
        return {
            "id": self.model,
            "object": "model",
            "created": 0,
            "owned_by": self.owned_by,
            "provider": self.project_id,
            "supported_endpoints": ["/v1/chat/completions"],
            **deepcopy(self.capabilities),
        }


class ModelGateway:
    """Register real models once at startup; never infer a platform from its model name."""

    def __init__(self):
        self._models: dict[str, ModelDescriptor] = {}

    def register(
        self,
        model: str,
        provider: ChatProvider,
        *,
        owned_by: str,
        capabilities: dict[str, Any],
        project_id: str | None = None,
    ) -> None:
        if not model or model in self._models:
            raise ValueError("Model is empty or already registered")
        if not owned_by or not isinstance(capabilities, dict):
            raise ValueError("Model owner and capabilities are required")
        if "supported_endpoints" in capabilities:
            raise ValueError("supported_endpoints is managed by the gateway")
        from ..core.model_catalog import MODEL_CONFIG
        from ..core.public_models import PUBLIC_MODELS

        if model in MODEL_CONFIG or model in PUBLIC_MODELS:
            raise ValueError("Model conflicts with an existing Flow model")
        self._models[model] = ModelDescriptor(
            model=model,
            provider=provider,
            project_id=project_id or owned_by,
            owned_by=owned_by,
            capabilities=deepcopy(capabilities),
        )

    def provider(self, model: str) -> ChatProvider | None:
        record = self._models.get(model)
        return record.provider if record else None

    def models(self) -> list[dict[str, Any]]:
        return [record.as_api_model() for record in self._models.values()]
