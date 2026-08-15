"""Keep the release-gate decision kernel independent of delivery mechanisms."""

import ast
from pathlib import Path


def test_shadow_campaign_domain_has_no_framework_or_adapter_imports() -> None:
    package = Path(__file__).parents[2] / "sana" / "modules" / "shadow_campaign"
    forbidden = (
        "sana.app",
        "sana.platform",
        "fastapi",
        "sqlalchemy",
        "httpx",
        "requests",
        "docker",
    )
    violations: list[str] = []

    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
            for name in names:
                if name.startswith(forbidden):
                    violations.append(f"{path.name}:{node.lineno}:{name}")

    assert violations == []
