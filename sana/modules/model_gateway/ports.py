"""Provider, secret and structured-output contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, TypeVar

from sana.modules.model_gateway.domain import (
    ModelInvocationContext,
    ModelInvocationReservation,
    ModelRequest,
    ProviderResponse,
    RedactedInvocationError,
    ReusedModelResponse,
)


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


class ModelInvocationAuditSink(Protocol):
    async def reuse(
        self,
        context: ModelInvocationContext,
        request: ModelRequest,
        *,
        provider: str,
        call_no: int,
        logical_call_key: str,
        deadline: datetime,
    ) -> ReusedModelResponse | None: ...

    async def start(
        self,
        context: ModelInvocationContext,
        request: ModelRequest,
        *,
        provider: str,
        call_no: int,
        logical_call_key: str,
        deadline: datetime,
    ) -> ModelInvocationReservation: ...

    async def complete(
        self,
        reservation: ModelInvocationReservation,
        context: ModelInvocationContext,
        response: ProviderResponse,
    ) -> None: ...

    async def fail(
        self,
        reservation: ModelInvocationReservation,
        context: ModelInvocationContext,
        error: RedactedInvocationError,
    ) -> None: ...
