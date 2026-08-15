from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest

from sana.app.shadow_report import ShadowReportService
from sana.modules.identity.domain import Principal
from sana.modules.shadow_campaign.domain import CampaignStatus, GateStatus
from sana.modules.shadow_campaign.report import FinalReportReceipt
from sana.modules.shared.errors import InvariantViolation
from sana.platform.storage.campaign_reports import LocalCampaignReportStore

from tests.test_modules.shadow_campaign.test_report import (
    OWNER,
    TENANT,
    report_snapshot,
)


class FakeReportGateway:
    def __init__(self, snapshot, *, stale_once: bool = False) -> None:
        self.snapshot = snapshot
        self.stale_once = stale_once
        self.read_count = 0
        self.bind_count = 0
        self.binding = None
        self._lock = asyncio.Lock()

    async def read(self, tenant_id, user_id, campaign_id):
        self.read_count += 1
        if (
            tenant_id != self.snapshot.tenant_id
            or user_id != self.snapshot.owner_user_id
            or campaign_id != self.snapshot.campaign_id
        ):
            return None
        if self.binding is None:
            return self.snapshot
        return replace(
            self.snapshot,
            campaign_status=CampaignStatus.COMPLETED,
            campaign_version=self.snapshot.campaign_version + 1,
            existing_final_binding={
                "gate_status": self.binding.gate_status.value,
                "decision_input_hash": self.binding.decision_input_hash,
                "decision_hash": self.binding.decision_hash,
                "json_uri": self.binding.json_uri,
                "json_sha256": self.binding.json_sha256,
                "markdown_uri": self.binding.markdown_uri,
                "markdown_sha256": self.binding.markdown_sha256,
            },
        )

    async def bind(self, binding):
        async with self._lock:
            self.bind_count += 1
            if self.stale_once:
                self.stale_once = False
                raise InvariantViolation("changed", code="report_input_stale")
            duplicate = self.binding is not None
            if duplicate and self.binding != binding:
                raise AssertionError("concurrent finalization diverged")
            self.binding = self.binding or binding
            return FinalReportReceipt(
                binding.campaign_id,
                binding.gate_status,
                binding.decision_hash,
                binding.json_uri,
                binding.json_sha256,
                binding.markdown_uri,
                binding.markdown_sha256,
                duplicate,
            )


@pytest.mark.asyncio
async def test_pending_report_is_returned_but_never_persisted(tmp_path) -> None:
    gateway = FakeReportGateway(report_snapshot(result_count=5))
    store = LocalCampaignReportStore(tmp_path)
    service = ShadowReportService(gateway, store)

    result = await service.generate(
        Principal(TENANT, OWNER, "test", str(OWNER)),
        gateway.snapshot.campaign_id,
    )

    assert result is not None
    assert result.gate_status is GateStatus.PENDING
    assert result.final is False
    assert result.json_uri is None
    assert gateway.bind_count == 0
    assert not tuple(tmp_path.rglob("*"))


@pytest.mark.asyncio
async def test_stale_input_rebuilds_and_artifact_writes_converge(tmp_path) -> None:
    gateway = FakeReportGateway(report_snapshot(), stale_once=True)
    service = ShadowReportService(gateway, LocalCampaignReportStore(tmp_path))

    result = await service.generate(
        Principal(TENANT, OWNER, "test", str(OWNER)),
        gateway.snapshot.campaign_id,
    )

    assert result is not None and result.final
    assert result.gate_status is GateStatus.PASS
    assert gateway.read_count == 2
    assert gateway.bind_count == 2
    assert len(tuple(path for path in tmp_path.rglob("*") if path.is_file())) == 2


@pytest.mark.asyncio
async def test_concurrent_finalizers_and_later_reads_share_one_binding(tmp_path) -> None:
    gateway = FakeReportGateway(report_snapshot())
    service = ShadowReportService(gateway, LocalCampaignReportStore(tmp_path))
    principal = Principal(TENANT, OWNER, "test", str(OWNER))

    first, second = await asyncio.gather(
        service.generate(principal, gateway.snapshot.campaign_id),
        service.generate(principal, gateway.snapshot.campaign_id),
    )
    third = await service.generate(principal, gateway.snapshot.campaign_id)

    assert first is not None and second is not None and third is not None
    assert {first.decision_hash, second.decision_hash, third.decision_hash} == {
        first.decision_hash
    }
    assert {first.json_uri, second.json_uri, third.json_uri} == {first.json_uri}
    assert third.duplicate is True
    assert len(tuple(path for path in tmp_path.rglob("*") if path.is_file())) == 2


@pytest.mark.asyncio
async def test_non_owner_cannot_discover_campaign(tmp_path) -> None:
    gateway = FakeReportGateway(report_snapshot())
    service = ShadowReportService(gateway, LocalCampaignReportStore(tmp_path))

    result = await service.generate(
        Principal(TENANT, uuid4(), "test", "other"),
        gateway.snapshot.campaign_id,
    )

    assert result is None
    assert gateway.bind_count == 0
