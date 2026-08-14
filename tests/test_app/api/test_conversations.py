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
