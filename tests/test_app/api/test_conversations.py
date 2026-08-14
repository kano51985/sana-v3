from uuid import uuid4

import pytest


pytestmark = pytest.mark.asyncio


async def test_message_submission_requires_auth_and_idempotency_key(api_context) -> None:
    conversation_id = uuid4()

    unauthenticated = await api_context.client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": "hello"},
        headers={"Idempotency-Key": "request-1"},
    )
    missing_key = await api_context.client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": "hello"},
        headers=api_context.auth,
    )

    assert unauthenticated.status_code == 401
    assert missing_key.status_code == 422


async def test_message_submission_uses_authenticated_tenant(api_context) -> None:
    conversation_id = uuid4()
    response = await api_context.client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": "current price?"},
        headers={**api_context.auth, "Idempotency-Key": "request-1"},
    )

    assert response.status_code == 202
    command = api_context.conversations.commands[0]
    assert command.tenant_id == api_context.principal.tenant_id
    assert command.user_id == api_context.principal.user_id
    assert command.conversation_id == conversation_id
    assert response.json()["status"] == "QUEUED"


async def test_authenticated_client_can_create_list_and_read_conversations(api_context) -> None:
    created = await api_context.client.post(
        "/api/v1/conversations",
        json={"title": "Architecture review"},
        headers=api_context.auth,
    )
    conversation_id = created.json()["id"]
    listed = await api_context.client.get(
        "/api/v1/conversations",
        headers=api_context.auth,
    )
    messages = await api_context.client.get(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers=api_context.auth,
    )

    assert created.status_code == 201
    assert listed.json()["conversations"][0]["id"] == conversation_id
    assert messages.json() == {
        "conversation_id": conversation_id,
        "messages": [],
    }


async def test_identity_endpoint_validates_the_bearer_token(api_context) -> None:
    response = await api_context.client.get("/api/v1/me", headers=api_context.auth)

    assert response.status_code == 200
    assert response.json()["tenant_id"] == str(api_context.principal.tenant_id)
    assert response.json()["user_id"] == str(api_context.principal.user_id)
