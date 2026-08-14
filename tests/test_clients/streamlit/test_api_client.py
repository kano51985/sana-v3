from __future__ import annotations

import json
from uuid import UUID

import httpx
import pytest

from sana.clients.streamlit.api_client import (
    SanaAPIClient,
    SanaAPIError,
    parse_sse_lines,
)


TENANT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
USER_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
CONVERSATION_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
RUN_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")


def test_api_client_sends_auth_and_idempotency_without_mode_override() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/me":
            return httpx.Response(
                200,
                json={
                    "tenant_id": TENANT_ID,
                    "user_id": USER_ID,
                    "issuer": "test",
                    "subject": USER_ID,
                },
            )
        if request.url.path.endswith("/messages"):
            return httpx.Response(
                202,
                json={
                    "message_id": USER_ID,
                    "response_run_id": TENANT_ID,
                    "search_run_id": str(RUN_ID),
                    "status": "QUEUED",
                    "duplicate": False,
                },
            )
        raise AssertionError(request.url)

    client = SanaAPIClient(
        "http://api.test",
        "secret-access-token",
        transport=httpx.MockTransport(handler),
    )

    identity = client.authenticate()
    submitted = client.submit_message(
        CONVERSATION_ID,
        "Find the latest facts",
        idempotency_key="stable-request",
    )

    assert identity["tenant_id"] == TENANT_ID
    assert submitted["search_run_id"] == str(RUN_ID)
    assert all(
        request.headers["authorization"] == "Bearer secret-access-token"
        for request in requests
    )
    message_request = requests[-1]
    assert message_request.headers["idempotency-key"] == "stable-request"
    assert json.loads(message_request.content) == {"content": "Find the latest facts"}
    assert "mode" not in message_request.content.decode("utf-8")


def test_api_client_covers_conversations_runs_cancel_and_evidence() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/conversations" and request.method == "GET":
            return httpx.Response(200, json={"conversations": [{"id": str(CONVERSATION_ID)}]})
        if path == "/api/v1/conversations" and request.method == "POST":
            return httpx.Response(201, json={"id": str(CONVERSATION_ID), "title": "New"})
        if path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"role": "USER", "content": "Hi"}]})
        if path == f"/api/v1/runs/{RUN_ID}" and request.method == "GET":
            return httpx.Response(200, json={"id": str(RUN_ID), "mode": "FAST"})
        if path == f"/api/v1/runs/{RUN_ID}/cancel":
            return httpx.Response(200, json={"id": str(RUN_ID), "status": "CANCELLED"})
        if path == f"/api/v1/runs/{RUN_ID}/evidence":
            return httpx.Response(
                200,
                json={"run_id": str(RUN_ID), "evidence": [], "missing_facts": []},
            )
        raise AssertionError((request.method, request.url))

    client = SanaAPIClient(
        "http://api.test",
        "token",
        transport=httpx.MockTransport(handler),
    )

    assert client.list_conversations()[0]["id"] == str(CONVERSATION_ID)
    assert client.create_conversation("New")["title"] == "New"
    assert client.list_messages(CONVERSATION_ID)[0]["role"] == "USER"
    assert client.get_run(RUN_ID)["mode"] == "FAST"
    assert client.cancel_run(RUN_ID)["status"] == "CANCELLED"
    assert client.get_evidence(RUN_ID)["missing_facts"] == []


def test_sse_parser_supports_multiline_data_and_ignores_heartbeats() -> None:
    events = list(
        parse_sse_lines(
            [
                ": heartbeat",
                "id: 4",
                "event: STEP_PROGRESS",
                'data: {"phase":',
                'data: "fetch"}',
                "",
            ]
        )
    )

    assert len(events) == 1
    assert events[0].sequence == 4
    assert events[0].payload == {"phase": "fetch"}


def test_sse_reconnect_uses_last_event_id_and_stops_on_terminal_event() -> None:
    cursors: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cursors.append(request.headers["last-event-id"])
        if len(cursors) == 1:
            body = 'id: 1\nevent: STEP_STARTED\ndata: {"step":"fetch"}\n\n'
        else:
            body = (
                'id: 2\nevent: RUN_COMPLETED\n'
                'data: {"terminal":true,"answer":"done"}\n\n'
            )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    client = SanaAPIClient(
        "http://api.test",
        "token",
        transport=httpx.MockTransport(handler),
    )

    events = list(
        client.iter_run_events(
            RUN_ID,
            reconnect_delay_seconds=0,
        )
    )

    assert cursors == ["0", "1"]
    assert [event.sequence for event in events] == [1, 2]
    assert events[-1].is_terminal is True


def test_api_errors_do_not_expose_access_tokens() -> None:
    client = SanaAPIClient(
        "http://api.test",
        "top-secret-token",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(401, json={"detail": "Unauthorized"})
        ),
    )

    with pytest.raises(SanaAPIError) as captured:
        client.authenticate()

    assert captured.value.status_code == 401
    assert "top-secret-token" not in str(captured.value)
    assert str(captured.value) == "Unauthorized"


@pytest.mark.parametrize(
    "url",
    ["localhost:8000", "file:///tmp/socket", "http://user:pass@localhost:8000"],
)
def test_api_url_rejects_relative_non_http_and_embedded_credentials(url: str) -> None:
    with pytest.raises(ValueError):
        SanaAPIClient(url, "token")
