from pathlib import Path


def test_compose_separates_database_owner_from_runtime_role() -> None:
    compose = Path("deployment/docker-compose.yml").read_text(encoding="utf-8")

    assert "provision-db-role:" in compose
    assert "NOSUPERUSER NOBYPASSRLS" in Path(
        "deployment/postgres/provision-app-role.sql"
    ).read_text(encoding="utf-8")
    assert "postgresql+asyncpg://${POSTGRES_APP_USER:-sana_app}:" in compose
    assert "SANA_DATABASE_URL: postgresql+asyncpg://sana:" in compose
    assert "condition: service_completed_successfully" in compose
