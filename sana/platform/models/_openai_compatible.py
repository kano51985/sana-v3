"""Shared HTTP adapter for OpenAI-compatible chat-completions endpoints."""

from __future__ import annotations

from typing import Any

import httpx

from sana.modules.model_gateway.domain import (
    ModelOutputError,
    ModelRequest,
    OutputFormat,
    ProviderResponse,
)
from sana.modules.model_gateway.ports import SecretProvider
from sana.modules.shared.errors import ErrorCategory, TypedError
from sana.platform.security.secrets import require_secret


class ModelProviderFailure(TypedError):
    pass


class OpenAICompatibleModelProvider:
    def __init__(
        self,
        *,
        base_url: str,
        secret_provider: SecretProvider | None,
        secret_name: str | None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._secrets = secret_provider
        self._secret_name = secret_name
        self._client = client or httpx.AsyncClient(follow_redirects=False)
        self._owns_client = client is None

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._secret_name is not None:
            if self._secrets is None:
                raise ValueError("A SecretProvider is required for this model provider")
            headers["Authorization"] = (
                f"Bearer {require_secret(self._secrets, self._secret_name)}"
            )
        return headers

    def _request_payload(self, request: ModelRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [
                {"role": message.role.value, "content": message.content}
                for message in request.messages
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
        }
        if request.output_format is OutputFormat.JSON_OBJECT:
            payload["response_format"] = {"type": "json_object"}
        return payload

    async def invoke(
        self,
        request: ModelRequest,
        *,
        timeout_seconds: float,
    ) -> ProviderResponse:
        payload = self._request_payload(request)
        try:
            response = await self._client.post(
                f"{self._base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
                timeout=httpx.Timeout(timeout_seconds),
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ModelProviderFailure(
                ErrorCategory.TRANSIENT,
                "model_network_failure",
                str(exc) or "Model provider network failure",
                retryable=True,
                cause=exc,
            ) from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise ModelProviderFailure(
                ErrorCategory.TRANSIENT,
                f"model_http_{response.status_code}",
                f"Model provider returned HTTP {response.status_code}",
                retryable=True,
            )
        if response.status_code >= 400:
            raise ModelProviderFailure(
                ErrorCategory.PERMANENT,
                f"model_http_{response.status_code}",
                f"Model provider returned HTTP {response.status_code}",
                retryable=False,
            )
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise TypeError("response body must be an object")
            text = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            if not isinstance(usage, dict):
                raise TypeError("usage must be an object")
            model = str(data.get("model") or request.model)
            prompt_tokens = int(usage.get("prompt_tokens", 0))
            completion_tokens = int(usage.get("completion_tokens", 0))
            response_id_value = data.get("id")
            response_id = (
                response_id_value
                if isinstance(response_id_value, str) and response_id_value.strip()
                else None
            )
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ModelOutputError(f"Model provider returned invalid response data: {exc}") from exc
        if not isinstance(text, str) or not text.strip():
            raise ModelOutputError("Model provider returned empty response content")
        return ProviderResponse(
            text=text,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            response_id=response_id,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
