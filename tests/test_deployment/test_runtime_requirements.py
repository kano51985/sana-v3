from pathlib import Path
import tomllib


def test_container_runtime_requirements_match_core_project_dependencies() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    expected = set(project["project"]["dependencies"])
    actual = {
        line.strip()
        for line in Path("deployment/requirements-runtime.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert actual == expected


def test_legacy_and_memory_migration_dependencies_are_not_in_runtime_image() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    runtime = set(project["project"]["dependencies"])

    assert not any(item.startswith("chromadb") for item in runtime)
    assert not any(item.startswith("pymongo") for item in runtime)
    for extra in ("legacy", "migration"):
        dependencies = project["project"]["optional-dependencies"][extra]
        assert any(item.startswith("chromadb") for item in dependencies)
        assert any(item.startswith("pymongo") for item in dependencies)
