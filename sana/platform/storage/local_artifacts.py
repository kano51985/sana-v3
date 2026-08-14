"""Tenant-scoped, content-addressed artifact storage on a shared filesystem."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import UUID, uuid4

from sana.modules.orchestration.domain import ArtifactRef
from sana.modules.shared.errors import ErrorCategory, TypedError


class LocalArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).expanduser().resolve()

    def _path(self, tenant_id: UUID, run_id: UUID, digest: str) -> Path:
        return self._root / str(tenant_id) / str(run_id) / digest[:2] / digest

    @staticmethod
    def _uri(tenant_id: UUID, run_id: UUID, digest: str) -> str:
        return f"artifact://{tenant_id}/{run_id}/{digest}"

    @staticmethod
    def _parse(reference: ArtifactRef) -> tuple[UUID, UUID, str]:
        parsed = urlparse(reference.uri)
        parts = tuple(part for part in parsed.path.split("/") if part)
        if parsed.scheme != "artifact" or len(parts) != 2:
            raise TypedError(
                ErrorCategory.CONTENT,
                "invalid_artifact_uri",
                "Artifact reference URI is invalid",
                retryable=False,
            )
        try:
            tenant_id = UUID(parsed.netloc)
            run_id = UUID(parts[0])
        except ValueError as exc:
            raise TypedError(
                ErrorCategory.CONTENT,
                "invalid_artifact_uri",
                "Artifact reference identity is invalid",
                retryable=False,
                cause=exc,
            ) from exc
        digest = parts[1]
        if digest != reference.sha256:
            raise TypedError(
                ErrorCategory.CONTENT,
                "artifact_digest_mismatch",
                "Artifact URI and digest do not match",
                retryable=False,
            )
        return tenant_id, run_id, digest

    async def put_bytes(
        self,
        tenant_id: UUID,
        run_id: UUID,
        payload: bytes,
    ) -> ArtifactRef:
        if not isinstance(payload, bytes) or not payload:
            raise ValueError("Artifact payload must be non-empty bytes")
        digest = hashlib.sha256(payload).hexdigest()
        path = self._path(tenant_id, run_id, digest)
        await asyncio.to_thread(self._write_atomic, path, payload, digest)
        return ArtifactRef(self._uri(tenant_id, run_id, digest), digest)

    @staticmethod
    def _write_atomic(
        path: Path,
        payload: bytes,
        digest: str,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            existing = path.read_bytes()
            if hashlib.sha256(existing).hexdigest() != digest:
                raise TypedError(
                    ErrorCategory.CONTENT,
                    "artifact_corrupted",
                    "Existing artifact failed its content hash check",
                    retryable=False,
                )
            return
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    async def get_bytes(self, tenant_id: UUID, reference: ArtifactRef) -> bytes:
        reference_tenant, run_id, digest = self._parse(reference)
        if reference_tenant != tenant_id:
            raise TypedError(
                ErrorCategory.PERMANENT,
                "artifact_tenant_mismatch",
                "Artifact does not belong to the active tenant",
                retryable=False,
            )
        path = self._path(reference_tenant, run_id, digest)
        try:
            payload = await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError as exc:
            raise TypedError(
                ErrorCategory.CONTENT,
                "artifact_not_found",
                "Artifact payload is unavailable",
                retryable=True,
                cause=exc,
            ) from exc
        if hashlib.sha256(payload).hexdigest() != digest:
            raise TypedError(
                ErrorCategory.CONTENT,
                "artifact_corrupted",
                "Artifact failed its content hash check",
                retryable=False,
            )
        return payload

    async def put_json(
        self,
        tenant_id: UUID,
        run_id: UUID,
        payload: Any,
    ) -> ArtifactRef:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return await self.put_bytes(tenant_id, run_id, encoded)

    async def get_json(self, tenant_id: UUID, reference: ArtifactRef) -> Any:
        payload = await self.get_bytes(tenant_id, reference)
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TypedError(
                ErrorCategory.CONTENT,
                "artifact_json_invalid",
                "Artifact is not valid UTF-8 JSON",
                retryable=False,
                cause=exc,
            ) from exc
