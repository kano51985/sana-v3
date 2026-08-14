"""SSRF validation for syntax, DNS answers, redirect hops and connected peers."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import SplitResult, urlsplit

from sana.modules.shared.errors import ErrorCategory, TypedError


class SSRFBlocked(TypedError):
    def __init__(self, message: str) -> None:
        super().__init__(
            ErrorCategory.PERMANENT,
            "ssrf_blocked",
            message,
            retryable=False,
        )


class DNSResolver(Protocol):
    async def resolve(self, host: str, port: int) -> tuple[str, ...]: ...


class SystemDNSResolver:
    async def resolve(self, host: str, port: int) -> tuple[str, ...]:
        loop = asyncio.get_running_loop()
        results = await loop.getaddrinfo(
            host,
            port,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
        return tuple(dict.fromkeys(result[4][0] for result in results))


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    url: str
    host: str
    port: int
    addresses: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]


class SSRFGuard:
    _BLOCKED_SUFFIXES = (
        ".localhost",
        ".local",
        ".internal",
        ".home",
        ".lan",
        ".onion",
    )
    _BLOCKED_HOSTS = {
        "localhost",
        "metadata.google.internal",
        "metadata.azure.internal",
    }

    def __init__(
        self,
        resolver: DNSResolver | None = None,
        *,
        allowed_ports: frozenset[int] = frozenset({80, 443}),
        max_url_characters: int = 2_048,
    ) -> None:
        self._resolver = resolver or SystemDNSResolver()
        self._allowed_ports = allowed_ports
        self._max_url_characters = max_url_characters

    def validate_syntax(self, url: str) -> tuple[SplitResult, str, int]:
        if not url or len(url) > self._max_url_characters:
            raise SSRFBlocked("URL is empty or exceeds the length limit")
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise SSRFBlocked("URL contains an invalid host or port") from exc
        if parsed.scheme.lower() not in {"http", "https"}:
            raise SSRFBlocked("Only HTTP and HTTPS URLs are allowed")
        if parsed.username is not None or parsed.password is not None:
            raise SSRFBlocked("URL credentials are forbidden")
        if not parsed.hostname:
            raise SSRFBlocked("URL hostname is required")
        host = parsed.hostname.rstrip(".").lower()
        if "%" in host:
            raise SSRFBlocked("IPv6 zone identifiers are forbidden")
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise SSRFBlocked("URL hostname is not valid IDNA") from exc
        if host in self._BLOCKED_HOSTS or any(
            host.endswith(suffix) for suffix in self._BLOCKED_SUFFIXES
        ):
            raise SSRFBlocked("Local or internal hostnames are forbidden")
        resolved_port = port or (443 if parsed.scheme.lower() == "https" else 80)
        if resolved_port not in self._allowed_ports:
            raise SSRFBlocked("URL port is not allowed")
        return parsed, host, resolved_port

    @staticmethod
    def _public_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise SSRFBlocked("DNS returned an invalid IP address") from exc
        if not address.is_global:
            raise SSRFBlocked(f"Non-public IP address is forbidden: {address}")
        return address

    async def resolve_and_validate(self, url: str) -> ResolvedTarget:
        _, host, port = self.validate_syntax(url)
        try:
            literal = ipaddress.ip_address(host)
            values = (str(literal),)
        except ValueError:
            try:
                values = await self._resolver.resolve(host, port)
            except (OSError, socket.gaierror) as exc:
                raise TypedError(
                    ErrorCategory.TRANSIENT,
                    "dns_resolution_failed",
                    f"Could not resolve fetch hostname: {host}",
                    retryable=True,
                    cause=exc,
                ) from exc
        if not values:
            raise TypedError(
                ErrorCategory.TRANSIENT,
                "dns_resolution_empty",
                f"DNS returned no addresses for: {host}",
                retryable=True,
            )
        addresses = tuple(self._public_address(value) for value in values)
        return ResolvedTarget(url, host, port, addresses)

    def validate_peer(self, peer_address: str) -> None:
        self._public_address(peer_address)
