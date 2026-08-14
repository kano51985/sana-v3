"""Production secret sources; intentionally contains no user-registry fallback."""

from __future__ import annotations

import os
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from sana.modules.model_gateway.ports import SecretProvider
from sana.modules.shared.errors import ErrorCategory, TypedError


class SecretNotFound(TypedError):
    def __init__(self, name: str) -> None:
        super().__init__(
            ErrorCategory.PERMANENT,
            "secret_not_found",
            f"Required secret is not configured: {name}",
            retryable=False,
        )


class EnvironmentSecretProvider:
    def get_secret(self, name: str) -> str | None:
        value = os.environ.get(name)
        return value if value else None


class DirectorySecretProvider:
    """Read container-mounted secrets from one explicitly configured directory."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory.resolve()

    def get_secret(self, name: str) -> str | None:
        if not name or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for character in name):
            raise ValueError("Secret name contains unsupported characters")
        path = (self._directory / name).resolve()
        if path.parent != self._directory or not path.is_file():
            return None
        value = path.read_text(encoding="utf-8").strip()
        return value or None


class StaticSecretProvider:
    def __init__(self, secrets: Mapping[str, str]) -> None:
        self._secrets = MappingProxyType(dict(secrets))

    def get_secret(self, name: str) -> str | None:
        return self._secrets.get(name)


class ChainedSecretProvider:
    def __init__(self, *providers: SecretProvider) -> None:
        self._providers = providers

    def get_secret(self, name: str) -> str | None:
        for provider in self._providers:
            value = provider.get_secret(name)
            if value:
                return value
        return None


def require_secret(provider: SecretProvider, name: str) -> str:
    value = provider.get_secret(name)
    if not value:
        raise SecretNotFound(name)
    return value
