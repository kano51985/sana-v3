from __future__ import annotations

import httpx

from sana.platform.models._openai_compatible import OpenAICompatibleModelProvider


class LocalModelProvider(OpenAICompatibleModelProvider):
    def __init__(
        self,
        *,
        base_url: str = "http://localhost:11434/v1",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url,
            secret_provider=None,
            secret_name=None,
            client=client,
        )
