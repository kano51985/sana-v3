from __future__ import annotations

import httpx

from sana.modules.model_gateway.ports import SecretProvider
from sana.platform.models._openai_compatible import OpenAICompatibleModelProvider


class DeepSeekModelProvider(OpenAICompatibleModelProvider):
    def __init__(
        self,
        secret_provider: SecretProvider,
        *,
        base_url: str = "https://api.deepseek.com/v1",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url,
            secret_provider=secret_provider,
            secret_name="DEEPSEEK_API_KEY",
            client=client,
        )
