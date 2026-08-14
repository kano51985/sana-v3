"""Read Chroma text when recoverable; archive metadata for vector-only rows."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable

import chromadb

from sana.app.migration.readers.common import hash_file, manifest_hash
from sana.app.migration.service import (
    LegacyRecord,
    MigrationDisposition,
    ReaderResult,
    SourceManifest,
    canonical_hash,
)


class ChromaMemoryReader:
    def read(
        self,
        path: str | Path,
        *,
        collection_name: str = "sana_memories",
        batch_size: int = 500,
    ) -> ReaderResult:
        if batch_size < 1:
            raise ValueError("Chroma migration batch size must be positive")
        source_path = Path(path).resolve()
        if not source_path.is_dir():
            raise FileNotFoundError(f"Legacy Chroma directory does not exist: {source_path}")
        files = tuple(
            hash_file(item)
            for item in sorted(source_path.rglob("*"))
            if item.is_file()
        )
        client = chromadb.PersistentClient(path=str(source_path))
        collection = client.get_collection(collection_name)
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            payload = collection.get(
                limit=batch_size,
                offset=offset,
                include=["documents", "metadatas", "embeddings"],
            )
            ids = list(payload.get("ids") or ())
            if not ids:
                break
            documents = list(payload.get("documents") or [None] * len(ids))
            metadatas = list(payload.get("metadatas") or [None] * len(ids))
            embeddings = payload.get("embeddings")
            for index, source_id in enumerate(ids):
                rows.append(
                    {
                        "id": source_id,
                        "document": documents[index] if index < len(documents) else None,
                        "metadata": metadatas[index] if index < len(metadatas) else None,
                        "embedding": (
                            embeddings[index]
                            if embeddings is not None and index < len(embeddings)
                            else None
                        ),
                    }
                )
            offset += len(ids)
            if len(ids) < batch_size:
                break
        records = self.parse(rows)
        return ReaderResult(
            SourceManifest(
                "chroma_memory",
                f"{source_path}#{collection_name}",
                manifest_hash(files),
                files,
                len(records),
            ),
            records,
        )

    def parse(self, rows: Iterable[dict[str, Any]]) -> tuple[LegacyRecord, ...]:
        records: list[LegacyRecord] = []
        for row in rows:
            source_id = str(row.get("id", "")).strip()
            if not source_id:
                continue
            document = row.get("document")
            text = document.strip() if isinstance(document, str) else ""
            metadata = self._metadata(row.get("metadata"))
            embedding = row.get("embedding")
            dimensions = len(embedding) if embedding is not None else 0
            if text:
                material_hash = canonical_hash(
                    {"id": source_id, "document": text, "metadata": metadata}
                )
                records.append(
                    LegacyRecord(
                        "chroma_memory",
                        source_id,
                        str(metadata.get("memory_type") or "memory"),
                        text,
                        material_hash,
                        MigrationDisposition.IMPORT,
                        "recoverable_source_text_reembed",
                        {**metadata, "legacy_vector_dimensions": dimensions},
                    )
                )
            elif dimensions:
                vector_hash = self._vector_hash(embedding)
                records.append(
                    LegacyRecord(
                        "chroma_memory",
                        source_id,
                        "legacy_vector_archive",
                        None,
                        canonical_hash(
                            {
                                "id": source_id,
                                "metadata": metadata,
                                "dimensions": dimensions,
                                "vector_hash": vector_hash,
                            }
                        ),
                        MigrationDisposition.ARCHIVE,
                        "vector_without_source_text",
                        {
                            **metadata,
                            "legacy_vector_dimensions": dimensions,
                            "legacy_vector_hash": vector_hash,
                        },
                    )
                )
            else:
                records.append(
                    LegacyRecord(
                        "chroma_memory",
                        source_id,
                        "empty_memory",
                        None,
                        canonical_hash({"id": source_id, "metadata": metadata}),
                        MigrationDisposition.SKIP,
                        "empty_memory_record",
                        metadata,
                    )
                )
        return tuple(records)

    @staticmethod
    def _metadata(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        allowed = {"batch_id", "datetime", "entities", "memory_type"}
        return {str(key): item for key, item in value.items() if key in allowed}

    @staticmethod
    def _vector_hash(value: Any) -> str:
        digest = hashlib.sha256()
        if hasattr(value, "tobytes"):
            digest.update(value.tobytes())
        else:
            digest.update(canonical_hash(list(value)).encode("ascii"))
        return digest.hexdigest()
