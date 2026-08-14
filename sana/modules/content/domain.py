"""Content artifacts are distinct from search hits and snippets."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping
from uuid import UUID

from sana.modules.shared.errors import TypedError


class FetchMode(StrEnum):
    HTTP = "HTTP"
    KATANA = "KATANA"
    BROWSER = "BROWSER"


class FetchStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class FetchRequest:
    url: str
    deadline: datetime
    max_response_bytes: int = 5_000_000
    max_redirects: int = 3

    def __post_init__(self) -> None:
        if not self.url.strip():
            raise ValueError("Fetch URL cannot be empty")
        if self.deadline.tzinfo is None or self.deadline.utcoffset() is None:
            raise ValueError("Fetch deadline must be timezone-aware")
        if self.max_response_bytes < 1 or self.max_redirects < 0:
            raise ValueError("Fetch size and redirect limits are invalid")


@dataclass(frozen=True, slots=True)
class FetchArtifact:
    request_url: str
    final_url: str
    status: FetchStatus
    http_status: int | None
    media_type: str | None
    body: bytes
    content_hash: str | None
    fetched_at: datetime
    redirects: tuple[str, ...] = ()
    response_headers: Mapping[str, str] = field(default_factory=dict)
    error: TypedError | None = None

    def __post_init__(self) -> None:
        if self.fetched_at.tzinfo is None or self.fetched_at.utcoffset() is None:
            raise ValueError("Fetch timestamp must be timezone-aware")
        if self.status is FetchStatus.SUCCEEDED:
            expected = hashlib.sha256(self.body).hexdigest()
            if not self.body or self.content_hash != expected or self.error is not None:
                raise ValueError("Successful fetch artifact has invalid content state")
        elif self.body or self.content_hash is not None or self.error is None:
            raise ValueError("Failed fetch artifact cannot contain successful body state")
        object.__setattr__(
            self,
            "response_headers",
            MappingProxyType(dict(self.response_headers)),
        )


@dataclass(frozen=True, slots=True)
class ExtractedContent:
    canonical_url: str
    title: str
    text: str
    media_type: str
    language: str | None
    source_content_hash: str
    fetched_at: datetime
    extraction_metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("Extracted document text cannot be empty")
        if self.fetched_at.tzinfo is None or self.fetched_at.utcoffset() is None:
            raise ValueError("Extracted content timestamp must be timezone-aware")
        object.__setattr__(
            self,
            "extraction_metadata",
            MappingProxyType(dict(self.extraction_metadata)),
        )


@dataclass(frozen=True, slots=True)
class Document:
    id: UUID
    tenant_id: UUID
    canonical_url: str
    canonical_url_hash: str
    title: str
    source_host: str


@dataclass(frozen=True, slots=True)
class DocumentVersion:
    id: UUID
    tenant_id: UUID
    document_id: UUID
    content_hash: str
    text: str
    media_type: str
    language: str | None
    fetched_at: datetime

    def __post_init__(self) -> None:
        expected = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        if self.content_hash != expected:
            raise ValueError("DocumentVersion content hash does not match extracted text")


@dataclass(frozen=True, slots=True)
class DocumentChunk:
    ordinal: int
    text: str
    text_hash: str
    token_count: int
    start_offset: int
    end_offset: int

    def __post_init__(self) -> None:
        if self.ordinal < 0 or self.start_offset < 0 or self.end_offset <= self.start_offset:
            raise ValueError("Document chunk offsets are invalid")
        if self.text_hash != hashlib.sha256(self.text.encode("utf-8")).hexdigest():
            raise ValueError("Document chunk hash does not match text")
