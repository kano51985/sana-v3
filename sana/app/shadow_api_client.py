"""Redaction-safe asynchronous client for Shadow Campaign candidate traffic."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
import random
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from sana.modules.shadow_campaign.execution import CandidateSubmissionReceipt


_TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class ShadowAPIError(RuntimeError):
    """An intentionally body-free Candidate API failure."""

    def __init__(
        self,
        code: str,
        *,
        status_code: int | None,
        retryable: bool,
        request_may_have_committed: bool,
    ) -> None:
        super().__init__(f"Candidate API request failed ({code})")
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        self.request_may_have_committed = request_may_have_committed


@dataclass(frozen=True, slots=True)
class AuthenticatedIdentity:
    tenant_id: UUID
    user_id: UUID
    issuer: str
    subject: str


class ShadowCandidateAPI:
    def __init__(
        self,
        base_url: str,
        access_token: str,
        *,
        timeout_seconds: float = 15.0,
        max_retries: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random_source: Callable[[], float] = random.random,
    ) -> None:
        parsed = urlsplit(base_url.strip())
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("API URL must be an absolute credential-free HTTP(S) URL")
        token = access_token.strip()
        if not token:
            raise ValueError("Access token cannot be empty")
        if timeout_seconds <= 0:
            raise ValueError("Candidate API timeout must be positive")
        if not 0 <= max_retries <= 3:
            raise ValueError("Candidate API retries must be between zero and three")
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._sleeper = sleeper
        self._random = random_source
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(base_url={self._base_url!r})"

    async def __aenter__(self) -> "ShadowCandidateAPI":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def authenticate(self) -> AuthenticatedIdentity:
        payload = await self._request("GET", "/api/v1/me")
        try:
            return AuthenticatedIdentity(
                UUID(str(payload["tenant_id"])),
                UUID(str(payload["user_id"])),
                str(payload["issuer"]),
                str(payload["subject"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise self._invalid_response() from error

    async def create_conversation(
        self,
        *,
        title: str,
        idempotency_key: str,
    ) -> UUID:
        normalized_title = title.strip()
        normalized_key = idempotency_key.strip()
        if not normalized_title or not normalized_key:
            raise ValueError("Conversation title and idempotency key are required")
        payload = await self._request(
            "POST",
            "/api/v1/conversations",
            headers={"Idempotency-Key": normalized_key},
            json={"title": normalized_title},
        )
        try:
            return UUID(str(payload["id"]))
        except (KeyError, TypeError, ValueError) as error:
            raise self._invalid_response() from error

    async def submit_message(
        self,
        *,
        conversation_id: UUID,
        content: str,
        idempotency_key: str,
    ) -> CandidateSubmissionReceipt:
        normalized_content = content.strip()
        normalized_key = idempotency_key.strip()
        if not normalized_content or not normalized_key:
            raise ValueError("Message content and idempotency key are required")
        payload = await self._request(
            "POST",
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={"Idempotency-Key": normalized_key},
            json={"content": normalized_content},
        )
        try:
            return CandidateSubmissionReceipt(
                UUID(str(payload["message_id"])),
                UUID(str(payload["response_run_id"])),
                UUID(str(payload["search_run_id"])),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise self._invalid_response() from error

    async def get_run(self, run_id: UUID) -> Mapping[str, Any]:
        return await self._request("GET", f"/api/v1/runs/{run_id}")

    async def list_messages(self, conversation_id: UUID) -> tuple[Mapping[str, Any], ...]:
        payload = await self._request(
            "GET",
            f"/api/v1/conversations/{conversation_id}/messages",
        )
        values = payload.get("messages")
        if not isinstance(values, list) or any(not isinstance(item, Mapping) for item in values):
            raise self._invalid_response()
        return tuple(values)

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        may_commit = method.upper() not in {"GET", "HEAD", "OPTIONS"}
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.request(method, path, **kwargs)
            except (httpx.TimeoutException, httpx.NetworkError) as error:
                if attempt < self._max_retries:
                    await self._backoff(attempt)
                    continue
                raise ShadowAPIError(
                    "transport_exhausted",
                    status_code=None,
                    retryable=True,
                    request_may_have_committed=may_commit,
                ) from error
            if response.status_code in _TRANSIENT_STATUS:
                if attempt < self._max_retries:
                    await self._backoff(attempt)
                    continue
                raise ShadowAPIError(
                    "transient_exhausted",
                    status_code=response.status_code,
                    retryable=True,
                    request_may_have_committed=may_commit,
                )
            if not response.is_success:
                status = response.status_code
                code = (
                    "authentication_rejected"
                    if status in {401, 403}
                    else "idempotency_conflict"
                    if status == 409
                    else "request_rejected"
                )
                raise ShadowAPIError(
                    code,
                    status_code=status,
                    retryable=False,
                    request_may_have_committed=False,
                )
            try:
                payload = response.json()
            except ValueError as error:
                raise self._invalid_response() from error
            if not isinstance(payload, Mapping):
                raise self._invalid_response()
            return payload
        raise AssertionError("unreachable")

    async def _backoff(self, attempt: int) -> None:
        base = (0.5, 1.0, 2.0)[attempt]
        jitter = 0.8 + 0.4 * self._random()
        await self._sleeper(base * jitter)

    @staticmethod
    def _invalid_response() -> ShadowAPIError:
        return ShadowAPIError(
            "invalid_response",
            status_code=None,
            retryable=False,
            request_may_have_committed=False,
        )


__all__ = [
    "AuthenticatedIdentity",
    "ShadowAPIError",
    "ShadowCandidateAPI",
]
