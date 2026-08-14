from __future__ import annotations

from sana.clients.streamlit.session import (
    SESSION_PREFIX,
    get_value,
    initialize_session,
    is_authenticated,
    login_session,
    logout_session,
    set_value,
)


def test_browser_sessions_do_not_share_identity_conversation_or_messages() -> None:
    first: dict = {}
    second: dict = {}
    initialize_session(first, nonce_factory=lambda: "first")
    initialize_session(second, nonce_factory=lambda: "second")

    login_session(
        first,
        api_url="http://api.test",
        access_token="first-token",
        identity={"tenant_id": "tenant-a", "user_id": "user-a"},
    )
    login_session(
        second,
        api_url="http://api.test",
        access_token="second-token",
        identity={"tenant_id": "tenant-b", "user_id": "user-b"},
    )
    set_value(first, "selected_conversation_id", "conversation-a")
    get_value(first, "messages_by_conversation")["conversation-a"] = ["private"]

    assert get_value(second, "identity") == {
        "tenant_id": "tenant-b",
        "user_id": "user-b",
    }
    assert get_value(second, "selected_conversation_id") is None
    assert get_value(second, "messages_by_conversation") == {}
    assert get_value(first, "nonce") != get_value(second, "nonce")


def test_login_and_logout_clear_all_previous_user_data() -> None:
    state: dict = {}
    initialize_session(state, nonce_factory=lambda: "stable-session")
    login_session(
        state,
        api_url="http://api.test/",
        access_token="token-a",
        identity={"tenant_id": "tenant-a", "user_id": "user-a"},
    )
    set_value(state, "conversations", [{"id": "private"}])
    set_value(state, "active_run_id", "run-a")

    login_session(
        state,
        api_url="http://api.test",
        access_token="token-b",
        identity={"tenant_id": "tenant-b", "user_id": "user-b"},
    )

    assert get_value(state, "conversations") == []
    assert get_value(state, "active_run_id") is None
    assert is_authenticated(state)

    logout_session(state)

    assert not is_authenticated(state)
    assert get_value(state, "nonce") == "stable-session"
    assert get_value(state, "api_url") == "http://api.test"
    assert all(str(key).startswith(SESSION_PREFIX) for key in state)
