"""Authentication boundary; production OIDC and dev auth are adapters."""

from __future__ import annotations

from typing import Protocol

from sana.modules.identity.domain import Principal


class AuthProvider(Protocol):
    async def authenticate(self, bearer_token: str) -> Principal: ...
