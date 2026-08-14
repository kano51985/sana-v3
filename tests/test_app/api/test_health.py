import pytest


@pytest.mark.asyncio
async def test_injected_application_container_is_ready(api_context) -> None:
    response = await api_context.client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {"container": "externally_managed"},
    }
