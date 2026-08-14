"""Read only legacy raw dialogue batches; tool/search events are excluded."""

from __future__ import annotations

from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

from pymongo import MongoClient

from sana.app.migration.service import (
    LegacyRecord,
    MigrationDisposition,
    ReaderResult,
    SourceManifest,
    canonical_hash,
)


class MongoDialogueReader:
    def __init__(
        self,
        *,
        database: str = "sana_brain",
        collection: str = "raw_dialogue_batches",
    ) -> None:
        self._database = database
        self._collection = collection

    def read(self, uri: str = "mongodb://localhost:27017/") -> ReaderResult:
        client = MongoClient(
            uri,
            serverSelectionTimeoutMS=1_000,
            connectTimeoutMS=1_000,
        )
        try:
            client.admin.command("ping")
            documents = tuple(
                client[self._database][self._collection].find(
                    {},
                    {"dialogue_log": 1, "timestamp": 1},
                )
            )
        finally:
            client.close()
        records = self.parse(documents)
        source_location = self._safe_location(uri)
        source_hash = canonical_hash(
            [(record.source_id, record.source_hash) for record in records]
        )
        return ReaderResult(
            SourceManifest(
                "mongo_dialogue",
                f"{source_location}/{self._database}/{self._collection}",
                source_hash,
                (),
                len(records),
            ),
            records,
        )

    def parse(self, documents: Iterable[dict[str, Any]]) -> tuple[LegacyRecord, ...]:
        records: list[LegacyRecord] = []
        for document in documents:
            batch_id = str(document.get("_id", "")).strip()
            if not batch_id:
                continue
            messages = document.get("dialogue_log", ())
            if not isinstance(messages, list):
                messages = ()
            for index, message in enumerate(messages):
                if not isinstance(message, dict):
                    continue
                role = self._role(message.get("role"))
                content = message.get("content")
                source_id = f"{batch_id}:{index}"
                material = {"role": message.get("role"), "content": content}
                if role is None:
                    records.append(
                        LegacyRecord(
                            "mongo_dialogue",
                            source_id,
                            "excluded_event",
                            None,
                            canonical_hash(material),
                            MigrationDisposition.SKIP,
                            "excluded_tool_or_unknown_role",
                            {"batch_id": batch_id},
                        )
                    )
                    continue
                if not isinstance(content, str) or not content.strip():
                    records.append(
                        LegacyRecord(
                            "mongo_dialogue",
                            source_id,
                            "conversation_message",
                            None,
                            canonical_hash(material),
                            MigrationDisposition.SKIP,
                            "empty_message",
                            {"batch_id": batch_id, "role": role},
                        )
                    )
                    continue
                records.append(
                    LegacyRecord(
                        "mongo_dialogue",
                        source_id,
                        "conversation_message",
                        content.strip(),
                        canonical_hash(material),
                        MigrationDisposition.IMPORT,
                        "recoverable_dialogue_text",
                        {
                            "batch_id": batch_id,
                            "role": role,
                            "timestamp": document.get("timestamp"),
                        },
                    )
                )
        return tuple(records)

    @staticmethod
    def _role(value: Any) -> str | None:
        normalized = str(value or "").strip().casefold()
        if normalized in {"user", "白日"}:
            return "user"
        if normalized in {"assistant", "sana"}:
            return "assistant"
        return None

    @staticmethod
    def _safe_location(uri: str) -> str:
        parsed = urlsplit(uri)
        host = parsed.hostname or "localhost"
        netloc = host + (f":{parsed.port}" if parsed.port else "")
        return urlunsplit((parsed.scheme or "mongodb", netloc, "", "", ""))
