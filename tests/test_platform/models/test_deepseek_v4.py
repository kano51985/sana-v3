from __future__ import annotations

import json

import httpx
import pytest

from sana.modules.model_gateway.domain import (
    MessageRole,
    ModelMessage,
    ModelOutputError,
    ModelRequest,
    ModelRole,
    OutputFormat,
    ThinkingMode,
)
from sana.modules.shared.errors import ErrorCategory, TypedError
from sana.platform.models.deepseek import DeepSeekModelProvider
from sana.platform.security.secrets import StaticSecretProvider


def request() -> ModelRequest:
    return ModelRequest(
        ModelRole.VERIFIER,
        "deepseek-v4-flash",
        (ModelMessage(MessageRole.USER, "Return JSON."),),
        0.0,
        512,
        OutputFormat.JSON_OBJECT,
        ThinkingMode.DISABLED,
        "verifier-v1",
        "verdicts-v1",
    )


@pytest.mark.asyncio
async def test_deepseek_v4_payload_disables_thinking_and_requests_json() -> None:
    captured: dict[str, object] = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        captured["url"] = str(http_request.url)
        captured["payload"] = json.loads(http_request.content)
        return httpx.Response(
            200,
            json={
                "id": "response-safe-id",
                "model": "deepseek-v4-flash",
                "choices": [{"message": {"content": '{"verdicts":[]}'}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = DeepSeekModelProvider(
        StaticSecretProvider({"DEEPSEEK_API_KEY": "injected"}),
        client=client,
    )
    try:
        response = await provider.invoke(request(), timeout_seconds=2)
    finally:
        await client.aclose()

    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["payload"] == {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "Return JSON."}],
        "temperature": 0.0,
        "max_tokens": 512,
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
    }
    assert response.response_id == "response-safe-id"
    assert response.prompt_tokens == 7
    assert response.completion_tokens == 3
    assert not hasattr(response, "raw")


@pytest.mark.parametrize(
    ("status", "category", "retryable"),
    (
        (401, ErrorCategory.PERMANENT, False),
        (403, ErrorCategory.PERMANENT, False),
        (429, ErrorCategory.TRANSIENT, True),
        (503, ErrorCategory.TRANSIENT, True),
    ),
)
@pytest.mark.asyncio
async def test_deepseek_classifies_http_failures(
    status: int,
    category: ErrorCategory,
    retryable: bool,
) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, json={"error": "redacted"})
        )
    )
    provider = DeepSeekModelProvider(
        StaticSecretProvider({"DEEPSEEK_API_KEY": "injected"}),
        client=client,
    )
    try:
        with pytest.raises(TypedError) as captured:
            await provider.invoke(request(), timeout_seconds=2)
    finally:
        await client.aclose()

    assert captured.value.category is category
    assert captured.value.retryable is retryable


@pytest.mark.parametrize(
    "body",
    (
        {"choices": [{"message": {"content": ""}}]},
        {
            "choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": "invalid"},
        },
        [],
    ),
)
@pytest.mark.asyncio
async def test_deepseek_rejects_invalid_success_payload(body: object) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    )
    provider = DeepSeekModelProvider(
        StaticSecretProvider({"DEEPSEEK_API_KEY": "injected"}),
        client=client,
    )
    try:
        with pytest.raises(ModelOutputError):
            await provider.invoke(request(), timeout_seconds=2)
    finally:
        await client.aclose()
