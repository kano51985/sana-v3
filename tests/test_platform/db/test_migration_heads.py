from alembic.config import Config
from alembic.script import ScriptDirectory


def test_alembic_has_exactly_one_upgrade_head() -> None:
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))

    assert scripts.get_heads() == ["0006_merge_evidence_memory_heads"]
