from uuid import uuid4

import pytest

from sana.modules.orchestration.domain import ArtifactRef
from sana.modules.shared.errors import TypedError
from sana.platform.storage.local_artifacts import LocalArtifactStore


@pytest.mark.asyncio
async def test_artifact_round_trip_is_content_addressed_and_idempotent(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path)
    tenant_id = uuid4()
    run_id = uuid4()

    first = await store.put_json(tenant_id, run_id, {"message": "你好", "n": 1})
    second = await store.put_json(tenant_id, run_id, {"n": 1, "message": "你好"})

    assert first == second
    assert first.uri.startswith(f"artifact://{tenant_id}/{run_id}/")
    assert await store.get_json(tenant_id, first) == {"message": "你好", "n": 1}


@pytest.mark.asyncio
async def test_artifact_tenant_scope_is_enforced_before_disk_read(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path)
    owner = uuid4()
    reference = await store.put_bytes(owner, uuid4(), b"private")

    with pytest.raises(TypedError) as error:
        await store.get_bytes(uuid4(), reference)

    assert error.value.code == "artifact_tenant_mismatch"


@pytest.mark.asyncio
async def test_artifact_hash_is_verified_on_every_read(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path)
    tenant_id = uuid4()
    run_id = uuid4()
    reference = await store.put_bytes(tenant_id, run_id, b"expected")
    digest = reference.sha256
    path = tmp_path / str(tenant_id) / str(run_id) / digest[:2] / digest
    path.write_bytes(b"tampered")

    with pytest.raises(TypedError) as error:
        await store.get_bytes(tenant_id, reference)

    assert error.value.code == "artifact_corrupted"


@pytest.mark.asyncio
async def test_artifact_uri_cannot_escape_tenant_run_layout(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path)
    digest = "a" * 64
    malicious = ArtifactRef("artifact://not-a-uuid/../../secret", digest)

    with pytest.raises(TypedError) as error:
        await store.get_bytes(uuid4(), malicious)

    assert error.value.code == "invalid_artifact_uri"
