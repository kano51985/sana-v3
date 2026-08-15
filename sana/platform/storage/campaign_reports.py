"""Tenant/Campaign-scoped content-addressed storage for release reports."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse
from uuid import UUID, uuid4

from sana.modules.shared.errors import ErrorCategory, TypedError


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MEDIA_TYPES = frozenset({"application/json", "text/markdown"})


class LocalCampaignReportStore:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).expanduser().resolve()
        self._write_locks = tuple(Lock() for _ in range(64))

    def _path(self, tenant_id: UUID, campaign_id: UUID, digest: str) -> Path:
        if not _SHA256.fullmatch(digest):
            raise self._invalid("Campaign artifact digest is invalid")
        return (
            self._root
            / str(tenant_id)
            / str(campaign_id)
            / digest[:2]
            / digest
        )

    @staticmethod
    def _uri(tenant_id: UUID, campaign_id: UUID, digest: str) -> str:
        return f"campaign-artifact://{tenant_id}/{campaign_id}/{digest}"

    @staticmethod
    def _invalid(message: str) -> TypedError:
        return TypedError(
            ErrorCategory.CONTENT,
            "invalid_campaign_artifact_uri",
            message,
            retryable=False,
        )

    @classmethod
    def _parse(cls, uri: str) -> tuple[UUID, UUID, str]:
        parsed = urlparse(uri)
        parts = tuple(part for part in parsed.path.split("/") if part)
        if (
            parsed.scheme != "campaign-artifact"
            or parsed.params
            or parsed.query
            or parsed.fragment
            or len(parts) != 2
        ):
            raise cls._invalid("Campaign artifact URI is invalid")
        try:
            tenant_id = UUID(parsed.netloc)
            campaign_id = UUID(parts[0])
        except ValueError as error:
            raise cls._invalid("Campaign artifact identity is invalid") from error
        digest = parts[1]
        if not _SHA256.fullmatch(digest):
            raise cls._invalid("Campaign artifact digest is invalid")
        return tenant_id, campaign_id, digest

    async def put(
        self,
        tenant_id: UUID,
        campaign_id: UUID,
        payload: bytes,
        *,
        media_type: str,
    ) -> str:
        if not isinstance(payload, bytes) or not payload:
            raise ValueError("Campaign report payload must be non-empty bytes")
        if media_type not in _MEDIA_TYPES:
            raise ValueError("Campaign report media type is not allowlisted")
        digest = hashlib.sha256(payload).hexdigest()
        path = self._path(tenant_id, campaign_id, digest)
        await asyncio.to_thread(self._write_serialized, path, payload, digest)
        return self._uri(tenant_id, campaign_id, digest)

    def _write_serialized(self, path: Path, payload: bytes, digest: str) -> None:
        lock = self._write_locks[int(digest[:2], 16) % len(self._write_locks)]
        with lock:
            self._write_atomic(path, payload, digest)

    @staticmethod
    def _write_atomic(path: Path, payload: bytes, digest: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            existing = path.read_bytes()
            if hashlib.sha256(existing).hexdigest() != digest:
                raise TypedError(
                    ErrorCategory.CONTENT,
                    "campaign_artifact_corrupted",
                    "Existing Campaign artifact failed its content hash check",
                    retryable=False,
                )
            return
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                # The closed, fsynced temporary is linked into place atomically.
                # Unlike replace, this never opens a Windows sharing window on an
                # existing immutable content-addressed target.
                os.link(temporary, path)
            except FileExistsError:
                pass
            except OSError:
                # Filesystems without hard-link support retain the older atomic
                # replace fallback. Cross-process convergence remains hash-checked.
                try:
                    os.replace(temporary, path)
                except OSError:
                    if (
                        not path.exists()
                        or hashlib.sha256(path.read_bytes()).hexdigest() != digest
                    ):
                        raise
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise TypedError(
                    ErrorCategory.CONTENT,
                    "campaign_artifact_corrupted",
                    "Campaign artifact failed post-write verification",
                    retryable=False,
                )
        finally:
            temporary.unlink(missing_ok=True)

    async def get(
        self,
        tenant_id: UUID,
        campaign_id: UUID,
        uri: str,
    ) -> bytes:
        uri_tenant, uri_campaign, digest = self._parse(uri)
        if uri_tenant != tenant_id or uri_campaign != campaign_id:
            raise TypedError(
                ErrorCategory.PERMANENT,
                "campaign_artifact_scope_mismatch",
                "Campaign artifact does not belong to the active scope",
                retryable=False,
            )
        path = self._path(uri_tenant, uri_campaign, digest)
        try:
            payload = await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError as error:
            raise TypedError(
                ErrorCategory.CONTENT,
                "campaign_artifact_not_found",
                "Campaign artifact payload is unavailable",
                retryable=True,
                cause=error,
            ) from error
        if hashlib.sha256(payload).hexdigest() != digest:
            raise TypedError(
                ErrorCategory.CONTENT,
                "campaign_artifact_corrupted",
                "Campaign artifact failed its content hash check",
                retryable=False,
            )
        return payload


__all__ = ["LocalCampaignReportStore"]
