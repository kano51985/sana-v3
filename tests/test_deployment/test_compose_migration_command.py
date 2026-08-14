from pathlib import Path


def test_compose_uses_alembic_cli_instead_of_shadowed_python_module() -> None:
    compose = Path("deployment/docker-compose.yml").read_text(encoding="utf-8")

    assert 'command: ["alembic", "upgrade", "head"]' in compose
    assert 'command: ["python", "-m", "alembic"' not in compose
