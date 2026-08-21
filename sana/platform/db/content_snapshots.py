"""Tenant-scoped read adapter for previously extracted live content."""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy import select

from sana.modules.content.domain import ReusableContentSnapshot
from sana.modules.shared.errors import ErrorCategory, TypedError
from sana.platform.db.models.search import (
    DocumentVersion,
    DocumentVersionFetch,
    FetchArtifact,
)
from sana.platform.db.uow import TenantUnitOfWorkFactory


def _valid_sha256(value: str) -> bool:
    try:
        return len(value) == 64 and int(value, 16) >= 0
    except ValueError:
        return False


def _redirects(metadata: Mapping[str, Any]) -> tuple[str, ...]:
    raw = metadata.get("redirects", ())
    if not isinstance(raw, (list, tuple)) or any(
        not isinstance(value, str) or not value.strip() for value in raw
    ):
        raise TypedError(
            ErrorCategory.CONTENT,
            "cache_metadata_invalid",
            "Reusable fetch redirect metadata is invalid",
            retryable=False,
        )
    return tuple(raw)


def _validate_storage_identity(
    storage_uri: str,
    *,
    tenant_id: UUID,
    run_id: UUID,
    digest: str,
) -> None:
    parsed = urlparse(storage_uri)
    parts = tuple(part for part in parsed.path.split("/") if part)
    try:
        uri_tenant = UUID(parsed.netloc)
        uri_run = UUID(parts[0]) if len(parts) == 2 else None
    except ValueError:
        uri_tenant = None
        uri_run = None
    if (
        parsed.scheme != "artifact"
        or len(parts) != 2
        or uri_tenant != tenant_id
        or uri_run != run_id
        or parts[-1] != digest
    ):
        raise TypedError(
            ErrorCategory.CONTENT,
            "cache_artifact_identity_invalid",
            "Reusable fetch artifact identity is invalid",
            retryable=False,
        )


class SqlContentSnapshotReader:
    """Return the newest successful, extracted, original live fetch."""

    def __init__(self, uow_factory: TenantUnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def latest_for_url(
        self,
        tenant_id: UUID,
        canonical_url_hash: str,
    ) -> ReusableContentSnapshot | None:
        if not _valid_sha256(canonical_url_hash):
            raise ValueError("Document reuse canonical URL hash is invalid")
        async with self._uow_factory(tenant_id) as uow:
            row = (
                await uow.session.execute(
                    select(
                        FetchArtifact.id.label("source_fetch_artifact_id"),
                        FetchArtifact.run_id.label("source_run_id"),
                        DocumentVersionFetch.document_version_id.label(
                            "source_document_version_id"
                        ),
                        FetchArtifact.url.label("request_url"),
                        FetchArtifact.http_status.label("http_status"),
                        FetchArtifact.media_type.label("media_type"),
                        FetchArtifact.content_hash.label("content_hash"),
                        FetchArtifact.storage_uri.label("storage_uri"),
                        FetchArtifact.fetched_at.label("fetched_at"),
                        FetchArtifact.fetch_metadata.label("fetch_metadata"),
                    )
                    .join(
                        DocumentVersionFetch,
                        (
                            DocumentVersionFetch.tenant_id
                            == FetchArtifact.tenant_id
                        )
                        & (
                            DocumentVersionFetch.run_id == FetchArtifact.run_id
                        )
                        & (
                            DocumentVersionFetch.fetch_artifact_id
                            == FetchArtifact.id
                        ),
                    )
                    .join(
                        DocumentVersion,
                        (
                            DocumentVersion.tenant_id
                            == DocumentVersionFetch.tenant_id
                        )
                        & (
                            DocumentVersion.id
                            == DocumentVersionFetch.document_version_id
                        ),
                    )
                    .where(
                        FetchArtifact.tenant_id == tenant_id,
                        DocumentVersionFetch.tenant_id == tenant_id,
                        DocumentVersion.tenant_id == tenant_id,
                        FetchArtifact.url_hash == canonical_url_hash,
                        FetchArtifact.status == "SUCCEEDED",
                        FetchArtifact.fetcher == "http",
                        FetchArtifact.http_status.is_not(None),
                        FetchArtifact.media_type.is_not(None),
                        FetchArtifact.content_hash.is_not(None),
                        FetchArtifact.storage_uri.is_not(None),
                    )
                    .order_by(
                        FetchArtifact.fetched_at.desc(),
                        FetchArtifact.id.desc(),
                    )
                    .limit(1)
                )
            ).one_or_none()
        if row is None:
            return None
        content_hash = str(row.content_hash)
        storage_uri = str(row.storage_uri)
        source_run_id = UUID(str(row.source_run_id))
        if not _valid_sha256(content_hash):
            raise TypedError(
                ErrorCategory.CONTENT,
                "cache_artifact_identity_invalid",
                "Reusable fetch content hash is invalid",
                retryable=False,
            )
        _validate_storage_identity(
            storage_uri,
            tenant_id=tenant_id,
            run_id=source_run_id,
            digest=content_hash,
        )
        metadata = row.fetch_metadata
        if not isinstance(metadata, Mapping):
            raise TypedError(
                ErrorCategory.CONTENT,
                "cache_metadata_invalid",
                "Reusable fetch metadata is invalid",
                retryable=False,
            )
        redirects = _redirects(metadata)
        request_url = str(row.request_url)
        return ReusableContentSnapshot(
            source_fetch_artifact_id=UUID(str(row.source_fetch_artifact_id)),
            source_run_id=source_run_id,
            source_document_version_id=UUID(
                str(row.source_document_version_id)
            ),
            request_url=request_url,
            final_url=redirects[-1] if redirects else request_url,
            http_status=int(row.http_status),
            media_type=str(row.media_type),
            content_hash=content_hash,
            storage_uri=storage_uri,
            fetched_at=row.fetched_at,
            redirects=redirects,
        )
