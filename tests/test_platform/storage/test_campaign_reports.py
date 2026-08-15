from __future__ import annotations

import asyncio
import hashlib
from uuid import uuid4

import pytest

from sana.modules.shared.errors import TypedError
from sana.platform.storage.campaign_reports import LocalCampaignReportStore


@pytest.mark.asyncio
async def test_campaign_report_store_is_scoped_atomic_and_content_addressed(tmp_path) -> None:
    store = LocalCampaignReportStore(tmp_path)
    tenant_id, campaign_id = uuid4(), uuid4()
    payload = b'{"decision":"PASS"}'

    first = await store.put(
        tenant_id,
        campaign_id,
        payload,
        media_type="application/json",
    )
    second = await store.put(
        tenant_id,
        campaign_id,
        payload,
        media_type="application/json",
    )

    assert first == second
    assert first.startswith(f"campaign-artifact://{tenant_id}/{campaign_id}/")
    assert first.endswith(hashlib.sha256(payload).hexdigest())
    assert await store.get(tenant_id, campaign_id, first) == payload
    assert not tuple(tmp_path.rglob("*.tmp"))


@pytest.mark.asyncio
async def test_concurrent_identical_report_writes_converge_without_replace_race(
    tmp_path,
) -> None:
    store = LocalCampaignReportStore(tmp_path)
    tenant_id, campaign_id = uuid4(), uuid4()
    payload = b'{"decision":"concurrent"}'

    results = await asyncio.gather(
        *(
            store.put(
                tenant_id,
                campaign_id,
                payload,
                media_type="application/json",
            )
            for _ in range(32)
        )
    )

    assert len(set(results)) == 1
    assert await store.get(tenant_id, campaign_id, results[0]) == payload
    assert len(tuple(path for path in tmp_path.rglob("*") if path.is_file())) == 1
    assert not tuple(tmp_path.rglob("*.tmp"))


@pytest.mark.asyncio
async def test_campaign_report_store_rejects_cross_scope_and_path_traversal(tmp_path) -> None:
    store = LocalCampaignReportStore(tmp_path)
    tenant_id, campaign_id = uuid4(), uuid4()
    uri = await store.put(
        tenant_id,
        campaign_id,
        b"safe",
        media_type="text/markdown",
    )

    with pytest.raises(TypedError) as wrong_tenant:
        await store.get(uuid4(), campaign_id, uri)
    assert wrong_tenant.value.code == "campaign_artifact_scope_mismatch"

    escaped = f"campaign-artifact://{tenant_id}/{campaign_id}/../../secret"
    with pytest.raises(TypedError) as traversal:
        await store.get(tenant_id, campaign_id, escaped)
    assert traversal.value.code == "invalid_campaign_artifact_uri"


@pytest.mark.asyncio
async def test_campaign_report_store_detects_corruption(tmp_path) -> None:
    store = LocalCampaignReportStore(tmp_path)
    tenant_id, campaign_id = uuid4(), uuid4()
    uri = await store.put(
        tenant_id,
        campaign_id,
        b"original",
        media_type="application/json",
    )
    digest = uri.rsplit("/", 1)[-1]
    path = tmp_path / str(tenant_id) / str(campaign_id) / digest[:2] / digest
    path.write_bytes(b"corrupt")

    with pytest.raises(TypedError) as corrupted:
        await store.get(tenant_id, campaign_id, uri)
    assert corrupted.value.code == "campaign_artifact_corrupted"
