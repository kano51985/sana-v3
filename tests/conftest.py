"""Global safeguards for deterministic, offline-by-default tests."""

from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Iterator
from typing import Any

import pytest


_CREDENTIAL_NAMES = (
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
)


def _is_loopback(host: Any) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="ignore")
    host_text = str(host).strip("[]").lower()
    if host_text in {"", "localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(host_text).is_loopback
    except ValueError:
        return False


def _deny_external(host: Any) -> None:
    if not _is_loopback(host):
        raise AssertionError(
            f"External network is disabled during tests: {host!r}. "
            "Mark the test with @pytest.mark.live_network to opt in explicitly."
        )


@pytest.fixture(autouse=True)
def isolate_credentials(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Prevent tests from reading API keys from the user registry or shell."""

    monkeypatch.setenv("SANA_TESTING", "1")
    for name in _CREDENTIAL_NAMES:
        monkeypatch.delenv(name, raising=False)

    def test_env(name: str) -> str:
        return os.environ.get(name, "")

    monkeypatch.setattr("sana.models.credentials.get_user_env", test_env)
    monkeypatch.setattr("sana.models.deepseek_backend.get_user_env", test_env)
    monkeypatch.setattr("sana.models.openai_backend.get_user_env", test_env)
    yield


@pytest.fixture(autouse=True)
def block_external_network(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> Iterator[None]:
    """Deny real external sockets while retaining local integration testing."""

    if request.node.get_closest_marker("live_network") is not None:
        yield
        return

    real_getaddrinfo = socket.getaddrinfo
    real_create_connection = socket.create_connection
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any):
        _deny_external(host)
        return real_getaddrinfo(host, *args, **kwargs)

    def guarded_create_connection(address: Any, *args: Any, **kwargs: Any):
        _deny_external(address[0])
        return real_create_connection(address, *args, **kwargs)

    def guarded_connect(sock: socket.socket, address: Any) -> Any:
        if isinstance(address, tuple):
            _deny_external(address[0])
        return real_connect(sock, address)

    def guarded_connect_ex(sock: socket.socket, address: Any) -> int:
        if isinstance(address, tuple):
            _deny_external(address[0])
        return real_connect_ex(sock, address)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    yield
