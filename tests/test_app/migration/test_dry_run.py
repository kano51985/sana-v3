from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

from sana.app.migration.readers.chroma import ChromaMemoryReader
from sana.app.migration.readers.mongo import MongoDialogueReader
from sana.app.migration.readers.user_profile import UserProfileReader
from sana.app.migration.service import (
    MigrationDisposition,
    MigrationPlanner,
    ReaderResult,
    SourceManifest,
    canonical_hash,
)


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "migration"
TENANT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
USER_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


def _result(source: str, records) -> ReaderResult:
    return ReaderResult(
        SourceManifest(
            source_system=source,
            source_location=f"fixture://{source}",
            source_hash=canonical_hash(
                [(record.source_id, record.source_hash) for record in records]
            ),
            record_count=len(records),
        ),
        tuple(records),
    )


def test_dry_run_reports_redacted_counts_and_discard_reasons() -> None:
    profile = json.loads((FIXTURES / "user_profile.json").read_text(encoding="utf-8"))
    mongo = json.loads((FIXTURES / "mongo_dialogue.json").read_text(encoding="utf-8"))
    chroma = json.loads((FIXTURES / "chroma_rows.json").read_text(encoding="utf-8"))
    sources = (
        _result("user_profile", UserProfileReader().parse(profile)),
        _result("mongo_dialogue", MongoDialogueReader().parse(mongo)),
        _result("chroma_memory", ChromaMemoryReader().parse(chroma)),
    )

    plan = MigrationPlanner().build(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        sources=sources,
    )
    report = plan.safe_report()

    assert report["user_mapping"] == {
        "tenant_id": str(TENANT_ID),
        "user_id": str(USER_ID),
    }
    assert report["source_counts"] == {
        "chroma_memory": {"ARCHIVE": 1, "IMPORT": 1, "SKIP": 1},
        "mongo_dialogue": {"IMPORT": 2, "SKIP": 2},
        "user_profile": {"IMPORT": 4, "SKIP": 5},
    }
    assert report["discard_reasons"] == {
        "empty_memory_record": 1,
        "empty_message": 1,
        "excluded_configuration": 4,
        "excluded_secret": 1,
        "excluded_tool_or_unknown_role": 1,
        "vector_without_source_text": 1,
    }
    serialized = json.dumps(report, ensure_ascii=False)
    assert "never-migrate-this" not in serialized
    assert "private legacy query" not in serialized
    assert all("content" not in manifest for manifest in report["source_manifests"])


def test_only_recoverable_chroma_text_is_imported_and_vector_is_not_retained() -> None:
    rows = json.loads((FIXTURES / "chroma_rows.json").read_text(encoding="utf-8"))
    records = ChromaMemoryReader().parse(rows)

    text, vector_only, empty = records
    assert text.disposition is MigrationDisposition.IMPORT
    assert text.reason == "recoverable_source_text_reembed"
    assert "untrusted_field" not in text.metadata
    assert vector_only.disposition is MigrationDisposition.ARCHIVE
    assert vector_only.content is None
    assert vector_only.metadata["legacy_vector_dimensions"] == 2
    assert "embedding" not in vector_only.metadata
    assert empty.disposition is MigrationDisposition.SKIP


def test_conflicts_use_hashed_source_identity_and_reader_issues_are_reported() -> None:
    first = UserProfileReader().parse({"name": "First"})[0]
    second = UserProfileReader().parse({"name": "Second"})[0]
    sources = (
        ReaderResult(
            SourceManifest("user_profile", "one", canonical_hash("one"), record_count=1),
            (first,),
            ("fixture_warning",),
        ),
        ReaderResult(
            SourceManifest("user_profile", "two", canonical_hash("two"), record_count=1),
            (second,),
        ),
    )

    report = MigrationPlanner().build(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        sources=sources,
    ).safe_report()

    assert report["issues"] == ["fixture_warning"]
    assert len(report["conflicts"]) == 1
    assert "name" not in report["conflicts"][0]
    assert canonical_hash("name") in report["conflicts"][0]
