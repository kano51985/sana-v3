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
    celery_broker_url: str = "redis://localhost:6379/0"
    outbox_poll_interval_seconds: float = 0.5
    outbox_batch_size: int = 100
    reconciliation_redelivery_grace_seconds: float = 5.0
    artifact_root: str = "var/artifacts"
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
        if self.outbox_poll_interval_seconds <= 0:
            raise ValueError("Outbox poll interval must be positive")
        if self.outbox_batch_size < 1:
            raise ValueError("Outbox batch size must be positive")
        if self.reconciliation_redelivery_grace_seconds < 0:
            raise ValueError("Reconciliation redelivery grace cannot be negative")
        if not self.artifact_root.strip():
            raise ValueError("Artifact root cannot be empty")
        if self.environment == "production" and self.auth_mode == "dev":
            raise ValueError("Development authentication is forbidden in production")
        if self.auth_mode == "dev" and not self.dev_auth_enabled:
            raise ValueError("Development authentication must be explicitly enabled")
        if self.auth_mode == "oidc" and not all(
            (self.oidc_issuer, self.oidc_audience, self.oidc_jwks_url)
        ):
            raise ValueError("OIDC issuer, audience and JWKS URL are required")
        return self
