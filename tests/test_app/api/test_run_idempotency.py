from uuid import uuid4

import pytest


pytestmark = pytest.mark.asyncio


async def test_repeated_idempotency_key_returns_the_original_run(api_context) -> None:
    conversation_id = uuid4()
    headers = {**api_context.auth, "Idempotency-Key": "same-request"}

    first = await api_context.client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": "one fact"},
        headers=headers,
    )
    second = await api_context.client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": "one fact"},
        headers=headers,
    )

    assert first.status_code == second.status_code == 202
    assert first.json()["search_run_id"] == second.json()["search_run_id"]
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True


async def test_run_get_cancel_and_evidence_are_tenant_authorized(api_context) -> None:
    get_response = await api_context.client.get(
        f"/api/v1/runs/{api_context.run_id}",
        headers=api_context.auth,
    )
    cancel_response = await api_context.client.post(
        f"/api/v1/runs/{api_context.run_id}/cancel",
        headers=api_context.auth,
    )
    evidence_response = await api_context.client.get(
        f"/api/v1/runs/{api_context.run_id}/evidence",
        headers=api_context.auth,
    )
    missing_response = await api_context.client.get(
        f"/api/v1/runs/{uuid4()}",
        headers=api_context.auth,
    )

    assert get_response.json()["status"] == "RUNNING"
    assert get_response.json()["mode"] == "FAST"
    assert get_response.json()["routing_reason_codes"] == [
        "single_or_low_complexity_fact"
    ]
    assert cancel_response.json()["status"] == "CANCELLED"
    assert evidence_response.json() == {
        "run_id": str(api_context.run_id),
        "evidence": [],
        "missing_facts": [],
    }
    assert missing_response.status_code == 404
