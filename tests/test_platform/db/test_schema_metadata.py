from __future__ import annotations

import importlib.util
from pathlib import Path

from sqlalchemy import UniqueConstraint

from sana.platform.db.base import Base
from sana.platform.db.session import create_database_engine
import sana.platform.db.models  # noqa: F401


EXPECTED_TABLES = {
    "tenants",
    "users",
    "user_identities",
    "conversations",
    "messages",
    "response_runs",
    "search_runs",
    "search_steps",
    "step_attempts",
    "outbox_events",
    "run_events",
    "fact_requirements",
    "query_specs",
    "provider_attempts",
    "search_hits",
    "fetch_artifacts",
    "documents",
    "document_versions",
    "document_chunks",
    "evidence_candidates",
    "verified_evidence",
    "answer_claims",
    "citations",
    "memory_items",
    "memory_embeddings",
    "migration_ledger",
    "legacy_archives",
    "model_invocations",
    "shadow_campaigns",
    "shadow_run_results",
    "shadow_manual_reviews",
}
TENANT_TABLES = EXPECTED_TABLES - {"tenants"}


def _constraint_names(table_name: str) -> set[str]:
    return {
        constraint.name
        for constraint in Base.metadata.tables[table_name].constraints
        if constraint.name is not None
    }


def _index_names(table_name: str) -> set[str]:
    return {
        index.name
        for index in Base.metadata.tables[table_name].indexes
        if index.name is not None
    }


def _load_migration(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_metadata_contains_the_complete_platform_schema() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_every_user_data_table_has_required_tenant_boundary() -> None:
    for table_name in TENANT_TABLES:
        table = Base.metadata.tables[table_name]
        assert "tenant_id" in table.c, table_name
        assert table.c.tenant_id.nullable is False, table_name
        assert any(
            foreign_key.column.table.name == "tenants"
            for foreign_key in table.c.tenant_id.foreign_keys
        ), table_name


def test_workflow_recovery_constraints_and_indexes_exist() -> None:
    assert "uq_search_steps_run_revision_key" in _constraint_names("search_steps")
    assert "uq_step_attempts_step_number" in _constraint_names("step_attempts")
    assert "uq_step_attempts_idempotency_key" in _constraint_names("step_attempts")
    assert "ix_step_attempts_lease_scan" in _index_names("step_attempts")
    assert "ix_outbox_events_unpublished" in _index_names("outbox_events")
    outbox_index = next(
        index
        for index in Base.metadata.tables["outbox_events"].indexes
        if index.name == "ix_outbox_events_unpublished"
    )
    assert outbox_index.dialect_options["postgresql"]["where"] is not None


def test_document_and_memory_rows_have_tenant_local_identity_constraints() -> None:
    for table_name in (
        "documents",
        "document_versions",
        "document_chunks",
        "evidence_candidates",
        "verified_evidence",
        "memory_items",
        "model_invocations",
        "shadow_campaigns",
        "shadow_run_results",
        "shadow_manual_reviews",
    ):
        table = Base.metadata.tables[table_name]
        tenant_identity = [
            constraint
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
            and {column.name for column in constraint.columns} == {"tenant_id", "id"}
        ]
        assert tenant_identity, table_name


def test_evidence_candidates_persist_exact_quote_offsets() -> None:
    table = Base.metadata.tables["evidence_candidates"]

    assert table.c.start_offset.nullable is False
    assert table.c.end_offset.nullable is False
    assert "ck_evidence_candidates_quote_offsets" in _constraint_names(
        "evidence_candidates"
    )
    assert table.c.source_identity.nullable is False
    assert table.c.source_authority.nullable is False


def test_model_audit_and_citation_lineage_are_complete() -> None:
    audit = Base.metadata.tables["model_invocations"]
    citation = Base.metadata.tables["citations"]

    assert "uq_model_invocations_attempt_role_call" in _constraint_names(
        "model_invocations"
    )
    assert "ix_model_invocations_logical_call" in _index_names(
        "model_invocations"
    )
    for column in (
        "document_version_id",
        "document_chunk_id",
        "quote",
        "start_offset",
        "end_offset",
    ):
        assert citation.c[column].nullable is False
    for forbidden in ("prompt", "raw_request", "raw_response", "reasoning"):
        assert forbidden not in audit.c


def test_provider_attempt_identity_includes_provider() -> None:
    table = Base.metadata.tables["provider_attempts"]
    constraint = next(
        item
        for item in table.constraints
        if item.name == "uq_provider_attempts_query_provider_number"
    )

    assert {column.name for column in constraint.columns} == {
        "query_spec_id",
        "provider",
        "attempt_no",
    }


def test_migrations_cover_every_tenant_table_with_forced_rls() -> None:
    versions = Path(__file__).parents[3] / "alembic" / "versions"
    migration_paths = sorted(versions.glob("000*.py"))
    modules = [_load_migration(path) for path in migration_paths]
    protected = {
        table
        for module in modules
        for table in getattr(module, "TENANT_TABLES", ())
    }

    assert protected == TENANT_TABLES
    for path in migration_paths:
        module = _load_migration(path)
        if not getattr(module, "TENANT_TABLES", ()):
            continue
        source = path.read_text(encoding="utf-8")
        assert "ENABLE ROW LEVEL SECURITY" in source
        assert "FORCE ROW LEVEL SECURITY" in source
        assert "current_setting('app.tenant_id', true)" in source


def test_memory_embeddings_record_model_version() -> None:
    table = Base.metadata.tables["memory_embeddings"]

    assert table.c.model_version.nullable is False
    assert "uq_memory_embeddings_item_model_version" in _constraint_names(
        "memory_embeddings"
    )


def test_database_engine_is_postgres_only_and_normalizes_async_driver() -> None:
    engine = create_database_engine("postgresql://user:password@localhost/sana")
    try:
        assert engine.url.drivername == "postgresql+asyncpg"
    finally:
        engine.sync_engine.dispose()
