"""Environment-owned platform configuration."""

from __future__ import annotations

from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SanaSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SANA_",
        env_file=".env",
        extra="ignore",
    )

    environment: Literal["local", "test", "production"] = "local"
    database_url: str = "postgresql+asyncpg://sana:sana@localhost:5432/sana"
    redis_url: str = "redis://localhost:6379/1"
    auth_mode: Literal["dev", "oidc"] = "dev"
    dev_auth_enabled: bool = True
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_jwks_url: str = ""
    oidc_tenant_claim: str = "tenant_id"
    oidc_user_claim: str = "user_id"
    sse_block_ms: int = 15_000

    @model_validator(mode="after")
    def validate_auth(self) -> "SanaSettings":
        if self.environment == "production" and self.auth_mode == "dev":
            raise ValueError("Development authentication is forbidden in production")
        if self.auth_mode == "dev" and not self.dev_auth_enabled:
            raise ValueError("Development authentication must be explicitly enabled")
        if self.auth_mode == "oidc" and not all(
            (self.oidc_issuer, self.oidc_audience, self.oidc_jwks_url)
        ):
            raise ValueError("OIDC issuer, audience and JWKS URL are required")
        return self
