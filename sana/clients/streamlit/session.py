"""Per-browser Streamlit state with explicit identity reset boundaries."""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any, Callable
from uuid import uuid4

from sana.clients.streamlit.api_client import SanaAPIClient


SESSION_PREFIX = "sana_client_"


def _key(name: str) -> str:
    return f"{SESSION_PREFIX}{name}"


def initialize_session(
    state: MutableMapping[str, Any],
    *,
    default_api_url: str = "http://localhost:8000",
    nonce_factory: Callable[[], Any] = uuid4,
) -> None:
    defaults: dict[str, Any] = {
        "nonce": str(nonce_factory()),
        "api_url": default_api_url,
        "access_token": "",
        "identity": None,
        "conversations": [],
        "selected_conversation_id": None,
        "messages_by_conversation": {},
        "active_run_id": None,
        "active_run": None,
        "run_events": [],
        "last_event_id": 0,
        "selected_evidence_run_id": None,
        "show_evidence_after_run": True,
    }
    for name, value in defaults.items():
        state.setdefault(_key(name), value)


def get_value(state: MutableMapping[str, Any], name: str) -> Any:
    return state[_key(name)]


def set_value(state: MutableMapping[str, Any], name: str, value: Any) -> None:
    state[_key(name)] = value


def login_session(
    state: MutableMapping[str, Any],
    *,
    api_url: str,
    access_token: str,
    identity: dict[str, Any],
) -> None:
    _clear_user_data(state)
    set_value(state, "api_url", api_url.rstrip("/"))
    set_value(state, "access_token", access_token)
    set_value(state, "identity", dict(identity))


def logout_session(state: MutableMapping[str, Any]) -> None:
    api_url = get_value(state, "api_url")
    nonce = get_value(state, "nonce")
    for key in tuple(state):
        if str(key).startswith(SESSION_PREFIX):
            del state[key]
    initialize_session(state, default_api_url=api_url, nonce_factory=lambda: nonce)


def _clear_user_data(state: MutableMapping[str, Any]) -> None:
    for name, value in {
        "conversations": [],
        "selected_conversation_id": None,
        "messages_by_conversation": {},
        "active_run_id": None,
        "active_run": None,
        "run_events": [],
        "last_event_id": 0,
        "selected_evidence_run_id": None,
    }.items():
        set_value(state, name, value)


def is_authenticated(state: MutableMapping[str, Any]) -> bool:
    return bool(get_value(state, "access_token") and get_value(state, "identity"))


def client_credentials(state: MutableMapping[str, Any]) -> tuple[str, str]:
    return get_value(state, "api_url"), get_value(state, "access_token")


def build_api_client(state: MutableMapping[str, Any]) -> SanaAPIClient:
    api_url, access_token = client_credentials(state)
    return SanaAPIClient(api_url, access_token)
