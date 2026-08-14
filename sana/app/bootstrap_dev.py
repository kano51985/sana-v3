"""Create an idempotent local-development tenant and user."""

from __future__ import annotations

import argparse
import asyncio
from uuid import UUID

from sqlalchemy import select, text

from sana.app.settings import SanaSettings
from sana.platform.db.models.identity import Tenant, User
from sana.platform.db.session import create_database_engine, create_session_factory


DEFAULT_TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")
DEFAULT_USER_ID = UUID("00000000-0000-4000-8000-000000000002")


async def bootstrap(
    *,
    tenant_id: UUID = DEFAULT_TENANT_ID,
    user_id: UUID = DEFAULT_USER_ID,
) -> str:
    settings = SanaSettings()
    if settings.environment == "production" or settings.auth_mode != "dev":
        raise RuntimeError("Development identity bootstrap is forbidden in this mode")

    engine = create_database_engine(settings.database_url)
    sessions = create_session_factory(engine)
    try:
        async with sessions() as session:
            async with session.begin():
                tenant = await session.get(Tenant, tenant_id)
                if tenant is None:
                    session.add(
                        Tenant(
                            id=tenant_id,
                            slug="local-dev",
                            name="Local Development",
                            status="ACTIVE",
                        )
                    )
                    await session.flush()
                await session.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_id)},
                )
                user = await session.scalar(
                    select(User).where(
                        User.tenant_id == tenant_id,
                        User.id == user_id,
                    )
                )
                if user is None:
                    session.add(
                        User(
                            id=user_id,
                            tenant_id=tenant_id,
                            email="local@sana.invalid",
                            display_name="Local User",
                            status="ACTIVE",
                        )
                    )
        return f"{tenant_id}:{user_id}"
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", type=UUID, default=DEFAULT_TENANT_ID)
    parser.add_argument("--user-id", type=UUID, default=DEFAULT_USER_ID)
    args = parser.parse_args()
    token = asyncio.run(bootstrap(tenant_id=args.tenant_id, user_id=args.user_id))
    print(f"Local bearer token: {token}")


if __name__ == "__main__":
    main()
