"""Safe, read-only inventory command for legacy user-memory migration."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Callable
from uuid import NAMESPACE_URL, UUID, uuid5

from sana.app.migration.readers import (
    ChromaMemoryReader,
    MongoDialogueReader,
    UserProfileReader,
)
from sana.app.migration.service import (
    MigrationPlanner,
    ReaderResult,
    SourceManifest,
    canonical_hash,
)


_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_TENANT_ID = uuid5(NAMESPACE_URL, "sana://tenant/local")
_DEFAULT_USER_ID = uuid5(NAMESPACE_URL, "sana://user/legacy-profile")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inventory legacy Sana memory without changing PostgreSQL, MongoDB, "
            "Chroma, or the user profile."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate a redacted migration plan; this is the only supported mode.",
    )
    parser.add_argument(
        "--tenant-id",
        type=UUID,
        default=UUID(os.environ.get("SANA_MIGRATION_TENANT_ID", str(_DEFAULT_TENANT_ID))),
    )
    parser.add_argument(
        "--user-id",
        type=UUID,
        default=UUID(os.environ.get("SANA_MIGRATION_USER_ID", str(_DEFAULT_USER_ID))),
    )
    parser.add_argument(
        "--profile-path",
        type=Path,
        default=_PROJECT_ROOT / "user_profile.json",
    )
    parser.add_argument(
        "--chroma-path",
        type=Path,
        default=_PROJECT_ROOT / "sana_memory_db",
    )
    parser.add_argument(
        "--chroma-collection",
        default="sana_memories",
    )
    parser.add_argument(
        "--mongo-uri",
        default=os.environ.get("SANA_LEGACY_MONGO_URI", "mongodb://localhost:27017/"),
    )
    parser.add_argument("--mongo-database", default="sana_brain")
    parser.add_argument("--mongo-collection", default="raw_dialogue_batches")
    parser.add_argument("--migration-version", default="memory-v1")
    return parser


def _unavailable_source(source_system: str, source_location: str, error: Exception) -> ReaderResult:
    error_type = type(error).__name__
    return ReaderResult(
        manifest=SourceManifest(
            source_system=source_system,
            source_location=source_location,
            source_hash=canonical_hash(
                {"source_system": source_system, "status": "unavailable"}
            ),
            record_count=0,
        ),
        records=(),
        issues=(f"source_unavailable:{source_system}:{error_type}",),
    )


def _safe_read(
    source_system: str,
    source_location: str,
    read: Callable[[], ReaderResult],
) -> ReaderResult:
    try:
        return read()
    except Exception as error:  # A dry-run must still report all available sources.
        return _unavailable_source(source_system, source_location, error)


def create_plan(arguments: argparse.Namespace):
    profile_path = arguments.profile_path.resolve()
    chroma_path = arguments.chroma_path.resolve()
    sources = (
        _safe_read(
            "user_profile",
            str(profile_path),
            lambda: UserProfileReader().read(profile_path),
        ),
        _safe_read(
            "mongo_dialogue",
            "legacy-mongo",
            lambda: MongoDialogueReader(
                database=arguments.mongo_database,
                collection=arguments.mongo_collection,
            ).read(arguments.mongo_uri),
        ),
        _safe_read(
            "chroma_memory",
            str(chroma_path),
            lambda: ChromaMemoryReader().read(
                chroma_path,
                collection_name=arguments.chroma_collection,
            ),
        ),
    )
    return MigrationPlanner(migration_version=arguments.migration_version).build(
        tenant_id=arguments.tenant_id,
        user_id=arguments.user_id,
        sources=sources,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if not arguments.dry_run:
        parser.error("Only read-only --dry-run is supported; apply requires an injected store/embedder.")
    report = create_plan(arguments).safe_report()
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
