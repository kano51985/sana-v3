from pathlib import Path

import httpx
import pytest

from sana.modules.model_gateway.domain import (
    MessageRole,
    ModelMessage,
    ModelRequest,
    ModelRole,
)
from sana.platform.models.deepseek import DeepSeekModelProvider
from sana.platform.security.secrets import EnvironmentSecretProvider, StaticSecretProvider


def test_new_platform_contains_no_windows_registry_credential_path() -> None:
    platform_root = Path(__file__).parents[3] / "sana" / "platform"
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in platform_root.rglob("*.py")
    )
    assert "winreg" not in sources
    assert "get_user_env" not in sources


def test_environment_secret_provider_reads_process_environment_only(monkeypatch) -> None:
    provider = EnvironmentSecretProvider()
    assert provider.get_secret("DEEPSEEK_API_KEY") is None

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    assert provider.get_secret("DEEPSEEK_API_KEY") == "test-key"


@pytest.mark.asyncio
async def test_deepseek_uses_only_injected_secret_provider() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={
                "model": "deepseek-chat",
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = DeepSeekModelProvider(
        StaticSecretProvider({"DEEPSEEK_API_KEY": "injected"}),
        client=client,
    )
    try:
        response = await provider.invoke(
            ModelRequest(
                ModelRole.PLANNER,
                "deepseek-chat",
                (ModelMessage(MessageRole.USER, "hello"),),
                0.0,
                100,
            ),
            timeout_seconds=2.0,
        )
    finally:
        await client.aclose()

    assert captured["authorization"] == "Bearer injected"
    assert response.text == "ok"
    assert response.prompt_tokens == 2
