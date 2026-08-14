"""Production OIDC and explicit local-development authentication adapters."""

from __future__ import annotations

import asyncio
from uuid import UUID

import jwt
from jwt import PyJWKClient

from sana.modules.identity.domain import Principal
from sana.modules.shared.errors import ErrorCategory, TypedError


class AuthenticationError(TypedError):
    def __init__(self, message: str, *, code: str = "invalid_token") -> None:
        super().__init__(
            ErrorCategory.PERMANENT,
            code,
            message,
            retryable=False,
        )


class DevAuthProvider:
    """Accept `Bearer <tenant UUID>:<user UUID>` only in explicit dev mode."""

    async def authenticate(self, bearer_token: str) -> Principal:
        try:
            tenant_text, user_text = bearer_token.split(":", 1)
            tenant_id = UUID(tenant_text)
            user_id = UUID(user_text)
        except (ValueError, AttributeError) as exc:
            raise AuthenticationError(
                "Development token must be '<tenant UUID>:<user UUID>'"
            ) from exc
        return Principal(
            tenant_id=tenant_id,
            user_id=user_id,
            issuer="sana-dev",
            subject=str(user_id),
        )


class OIDCAuthProvider:
    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str,
        tenant_claim: str = "tenant_id",
        user_claim: str = "user_id",
    ) -> None:
        self._issuer = issuer.rstrip("/")
        self._audience = audience
        self._jwks = PyJWKClient(jwks_url, cache_keys=True)
        self._tenant_claim = tenant_claim
        self._user_claim = user_claim

    def _decode(self, token: str) -> dict:
        signing_key = self._jwks.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience=self._audience,
            issuer=self._issuer,
            options={"require": ["exp", "iat", "iss", "sub"]},
        )

    async def authenticate(self, bearer_token: str) -> Principal:
        try:
            claims = await asyncio.to_thread(self._decode, bearer_token)
            tenant_id = UUID(str(claims[self._tenant_claim]))
            user_id = UUID(str(claims[self._user_claim]))
            subject = str(claims["sub"])
        except (jwt.PyJWTError, KeyError, ValueError) as exc:
            raise AuthenticationError("OIDC token validation failed") from exc
        return Principal(
            tenant_id=tenant_id,
            user_id=user_id,
            issuer=self._issuer,
            subject=subject,
        )
