"""Import only user identity, relationships and preferences from profile JSON."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from sana.app.migration.readers.common import hash_file, manifest_hash
from sana.app.migration.service import (
    LegacyRecord,
    MigrationDisposition,
    ReaderResult,
    SourceManifest,
    canonical_hash,
)


class UserProfileReader:
    _SAFE_CATEGORIES = frozenset(
        {"gaming_preferences", "general_preferences", "relationships"}
    )
    _EXCLUDED_CATEGORIES = frozenset(
        {"model_config", "web_tool", "official_sources", "search_history"}
    )
    _SECRET_KEY = re.compile(r"(api.?key|token|secret|password|authorization)", re.I)

    def read(self, path: str | Path) -> ReaderResult:
        source_path = Path(path).resolve()
        data = json.loads(source_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Legacy user profile must contain a JSON object")
        backup = hash_file(source_path)
        records = self.parse(data)
        return ReaderResult(
            SourceManifest(
                "user_profile",
                str(source_path),
                manifest_hash((backup,)),
                (backup,),
                len(records),
            ),
            records,
        )

    def parse(self, data: dict[str, Any]) -> tuple[LegacyRecord, ...]:
        records: list[LegacyRecord] = []
        name = data.get("name")
        if isinstance(name, str) and name.strip():
            records.append(
                self._record(
                    "name",
                    "profile_identity",
                    name.strip(),
                    MigrationDisposition.IMPORT,
                    "recoverable_profile_identity",
                    {"field": "name"},
                )
            )
        for category in sorted(self._SAFE_CATEGORIES):
            values = data.get(category, {})
            if not isinstance(values, dict):
                records.append(
                    self._record(
                        category,
                        "profile_preference",
                        None,
                        MigrationDisposition.SKIP,
                        "invalid_preference_category",
                        {"category": category},
                        source_value=values,
                    )
                )
                continue
            for key, value in sorted(values.items(), key=lambda item: str(item[0])):
                key_text = str(key)
                if self._SECRET_KEY.search(key_text):
                    records.append(
                        self._record(
                            f"{category}:{key_text}",
                            "profile_preference",
                            None,
                            MigrationDisposition.SKIP,
                            "excluded_secret",
                            {"category": category, "key_hash": canonical_hash(key_text)},
                            source_value=value,
                        )
                    )
                    continue
                content = json.dumps(value, ensure_ascii=False, sort_keys=True)
                records.append(
                    self._record(
                        f"{category}:{key_text}",
                        "relationship" if category == "relationships" else "preference",
                        content,
                        MigrationDisposition.IMPORT,
                        "recoverable_profile_preference",
                        {"category": category, "key": key_text},
                    )
                )
        for category in sorted(self._EXCLUDED_CATEGORIES & set(data)):
            records.append(
                self._record(
                    category,
                    "excluded_configuration",
                    None,
                    MigrationDisposition.SKIP,
                    "excluded_configuration",
                    {"category": category},
                    source_value=data[category],
                )
            )
        return tuple(records)

    @staticmethod
    def _record(
        source_id: str,
        kind: str,
        content: str | None,
        disposition: MigrationDisposition,
        reason: str,
        metadata: dict[str, Any],
        *,
        source_value: Any | None = None,
    ) -> LegacyRecord:
        material = content if source_value is None else source_value
        return LegacyRecord(
            "user_profile",
            source_id,
            kind,
            content,
            canonical_hash(material),
            disposition,
            reason,
            metadata,
        )
