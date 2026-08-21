"""Content artifacts are distinct from search hits and snippets."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Iterable, Mapping
from uuid import UUID

from sana.modules.shared.errors import ErrorCategory, TypedError


ALLOWED_CONTENT_MEDIA_TYPES = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "text/plain",
        "text/csv",
        "application/json",
        "application/pdf",
    }
)


class FetchMode(StrEnum):
    HTTP = "HTTP"
    KATANA = "KATANA"
    BROWSER = "BROWSER"


class FetchStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class ReuseFreshness(StrEnum):
    STABLE = "STABLE"
    RECENT = "RECENT"
    CURRENT = "CURRENT"


class ReuseDecision(StrEnum):
    MISS = "MISS"
    CACHE_FRESH = "CACHE_FRESH"
    LIVE = "LIVE"
    CACHE_STALE_IF_ERROR = "CACHE_STALE_IF_ERROR"


@dataclass(frozen=True, slots=True)
class ReuseWindow:
    fresh_for: timedelta
    fallback_for: timedelta

    def __post_init__(self) -> None:
        if (
            self.fresh_for <= timedelta(0)
            or self.fallback_for <= timedelta(0)
            or self.fresh_for > self.fallback_for
        ):
            raise ValueError("Document reuse window is invalid")


@dataclass(frozen=True, slots=True)
class ReuseAssessment:
    freshness: ReuseFreshness
    age: timedelta
    decision: ReuseDecision
    fallback_eligible: bool


@dataclass(frozen=True, slots=True)
class ReusableContentSnapshot:
    source_fetch_artifact_id: UUID
    source_run_id: UUID
    source_document_version_id: UUID
    request_url: str
    final_url: str
    http_status: int
    media_type: str
    content_hash: str
    storage_uri: str
    fetched_at: datetime
    redirects: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.request_url.strip() or not self.final_url.strip():
            raise ValueError("Reusable snapshot URLs are required")
        if not self.storage_uri.strip():
            raise ValueError("Reusable snapshot storage URI is required")
        if self.media_type not in ALLOWED_CONTENT_MEDIA_TYPES:
            raise ValueError("Reusable snapshot media type is not allowed")
        if not 100 <= self.http_status <= 399:
            raise ValueError("Reusable snapshot HTTP status is invalid")
        try:
            valid_hash = (
                len(self.content_hash) == 64
                and int(self.content_hash, 16) >= 0
            )
        except ValueError:
            valid_hash = False
        if not valid_hash:
            raise ValueError("Reusable snapshot content hash is invalid")
        if self.fetched_at.tzinfo is None or self.fetched_at.utcoffset() is None:
            raise ValueError("Reusable snapshot timestamp must be timezone-aware")


@dataclass(frozen=True, slots=True)
class DocumentReusePolicy:
    version: str
    windows: Mapping[ReuseFreshness, ReuseWindow]

    _STRICTNESS = {
        ReuseFreshness.STABLE: 0,
        ReuseFreshness.RECENT: 1,
        ReuseFreshness.CURRENT: 2,
    }

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("Document reuse policy version is required")
        normalized = {
            ReuseFreshness(key): value for key, value in self.windows.items()
        }
        if set(normalized) != set(ReuseFreshness):
            raise ValueError("Document reuse policy must define every freshness")
        if not all(isinstance(value, ReuseWindow) for value in normalized.values()):
            raise TypeError("Document reuse policy windows are invalid")
        object.__setattr__(self, "windows", MappingProxyType(normalized))

    @classmethod
    def default(cls) -> "DocumentReusePolicy":
        return cls(
            "document-reuse-v1",
            {
                ReuseFreshness.STABLE: ReuseWindow(
                    timedelta(hours=24), timedelta(days=30)
                ),
                ReuseFreshness.RECENT: ReuseWindow(
                    timedelta(hours=6), timedelta(days=7)
                ),
                ReuseFreshness.CURRENT: ReuseWindow(
                    timedelta(minutes=15), timedelta(hours=2)
                ),
            },
        )

    def window_for(self, freshness: ReuseFreshness) -> ReuseWindow:
        return self.windows[ReuseFreshness(freshness)]

    def strictest(
        self,
        values: Iterable[ReuseFreshness],
    ) -> ReuseFreshness:
        normalized = tuple(ReuseFreshness(value) for value in values)
        if not normalized:
            raise ValueError("Document reuse requires at least one freshness")
        return max(normalized, key=self._STRICTNESS.__getitem__)

    def assess(
        self,
        freshness: ReuseFreshness,
        fetched_at: datetime,
        now: datetime,
    ) -> ReuseAssessment:
        if (
            fetched_at.tzinfo is None
            or fetched_at.utcoffset() is None
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            raise ValueError("Document reuse timestamps must be timezone-aware")
        if fetched_at > now:
            raise TypedError(
                ErrorCategory.CONTENT,
                "cache_timestamp_invalid",
                "Reusable content has a future fetch timestamp",
                retryable=False,
            )
        normalized = ReuseFreshness(freshness)
        age = now - fetched_at
        window = self.window_for(normalized)
        return ReuseAssessment(
            normalized,
            age,
            (
                ReuseDecision.CACHE_FRESH
                if age <= window.fresh_for
                else ReuseDecision.MISS
            ),
            age <= window.fallback_for,
        )

    @staticmethod
    def allows_stale_if_error(error: TypedError) -> bool:
        if error.code == "fetch_deadline_exceeded":
            return error.category is ErrorCategory.BUDGET
        if error.category is not ErrorCategory.TRANSIENT:
            return False
        if error.code in {
            "fetch_network_failure",
            "dns_resolution_failed",
            "dns_resolution_empty",
        }:
            return True
        if not error.code.startswith("fetch_http_"):
            return False
        try:
            status = int(error.code.removeprefix("fetch_http_"))
        except ValueError:
            return False
        return status == 429 or 500 <= status <= 599


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
