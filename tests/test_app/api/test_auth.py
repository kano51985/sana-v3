from uuid import uuid4

import pytest
from pydantic import ValidationError

from sana.app.api.auth import AuthenticationError, DevAuthProvider
from sana.app.settings import SanaSettings


@pytest.mark.asyncio
async def test_dev_auth_requires_explicit_uuid_pair() -> None:
    tenant_id, user_id = uuid4(), uuid4()
    principal = await DevAuthProvider().authenticate(f"{tenant_id}:{user_id}")

    assert principal.tenant_id == tenant_id
    assert principal.user_id == user_id
    with pytest.raises(AuthenticationError):
        await DevAuthProvider().authenticate("anonymous")


def test_production_configuration_rejects_dev_auth() -> None:
    with pytest.raises(ValidationError, match="forbidden in production"):
        SanaSettings(environment="production", auth_mode="dev")


def test_oidc_configuration_requires_issuer_audience_and_jwks() -> None:
    with pytest.raises(ValidationError, match="OIDC issuer"):
        SanaSettings(auth_mode="oidc")
