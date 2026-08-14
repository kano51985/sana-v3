import os
import socket

import pytest

from sana.models.deepseek_backend import DeepSeekBackend
from sana.models.openai_backend import OpenAIModelBackend


def test_test_mode_is_explicit() -> None:
    assert os.environ["SANA_TESTING"] == "1"


def test_real_user_credentials_are_not_visible() -> None:
    assert DeepSeekBackend()._get_key() == ""
    assert OpenAIModelBackend()._get_key() == ""


def test_external_dns_is_denied_by_default() -> None:
    with pytest.raises(AssertionError, match="External network is disabled"):
        socket.getaddrinfo("example.com", 443)


def test_loopback_dns_remains_available() -> None:
    assert socket.getaddrinfo("localhost", 80)
