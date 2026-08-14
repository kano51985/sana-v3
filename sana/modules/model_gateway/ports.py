"""Provider, secret and structured-output contracts."""

from __future__ import annotations

from typing import Any, Protocol, TypeVar

from sana.modules.model_gateway.domain import ModelRequest, ProviderResponse


T = TypeVar("T")


class ModelProvider(Protocol):
    async def invoke(
        self,
        request: ModelRequest,
        *,
        timeout_seconds: float,
    ) -> ProviderResponse: ...


class SecretProvider(Protocol):
    def get_secret(self, name: str) -> str | None: ...


class StructuredOutputParser(Protocol[T]):
    def parse(self, text: str) -> T: ...

    def repair_instruction(self, error: Exception) -> str: ...
