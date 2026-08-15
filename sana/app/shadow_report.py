"""Owner-authorized, crash-recoverable Shadow Campaign report finalization."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from sana.modules.identity.domain import Principal
from sana.modules.shadow_campaign.domain import GateStatus
from sana.modules.shadow_campaign.report import (
    CampaignReportBuilder,
    FinalReportBinding,
)
from sana.modules.shared.errors import InvariantViolation
from sana.platform.telemetry.redaction import ReportPrivacyGuard

if TYPE_CHECKING:
    from sana.modules.shadow_campaign.ports import (
        CampaignReportGateway,
        CampaignReportStore,
    )


@dataclass(frozen=True, slots=True)
class CampaignReportResult:
    campaign_id: UUID
    gate_status: GateStatus
    decision_state: str
    decision_input_hash: str
    decision_hash: str
    final: bool
    json_bytes: bytes
    markdown_bytes: bytes
    json_uri: str | None = None
    json_sha256: str | None = None
    markdown_uri: str | None = None
    markdown_sha256: str | None = None
    duplicate: bool = False


class ShadowReportService:
    def __init__(
        self,
        gateway: "CampaignReportGateway",
        store: "CampaignReportStore",
        builder: CampaignReportBuilder | None = None,
        *,
        max_stale_retries: int = 2,
    ) -> None:
        if max_stale_retries < 0 or max_stale_retries > 5:
            raise ValueError("Report stale retry count must be between zero and five")
        self._gateway = gateway
        self._store = store
        self._builder = builder or CampaignReportBuilder()
        self._max_stale_retries = max_stale_retries

    async def generate(
        self,
        principal: Principal,
        campaign_id: UUID,
    ) -> CampaignReportResult | None:
        for attempt in range(self._max_stale_retries + 1):
            snapshot = await self._gateway.read(
                principal.tenant_id,
                principal.user_id,
                campaign_id,
            )
            if snapshot is None:
                return None
            if snapshot.existing_final_binding is not None:
                return await self._read_existing(snapshot)

            prepared = self._builder.prepare(snapshot)
            ReportPrivacyGuard.validate_json_bytes(prepared.json_bytes)
            ReportPrivacyGuard.validate_text_bytes(prepared.markdown_bytes)
            if not prepared.finalizable:
                if prepared.decision.status is not GateStatus.PENDING:
                    raise InvariantViolation(
                        "Non-final Campaign report produced a terminal decision",
                        code="report_finalization_state_invalid",
                    )
                return CampaignReportResult(
                    campaign_id,
                    prepared.decision.status,
                    prepared.decision.decision_state,
                    prepared.decision_input_hash,
                    prepared.decision_hash,
                    False,
                    prepared.json_bytes,
                    prepared.markdown_bytes,
                )
            if prepared.decision.status is GateStatus.PENDING:
                raise InvariantViolation(
                    "Final Campaign report has no terminal gate decision",
                    code="report_finalization_state_invalid",
                )

            json_sha = hashlib.sha256(prepared.json_bytes).hexdigest()
            markdown_sha = hashlib.sha256(prepared.markdown_bytes).hexdigest()
            json_uri = await self._store.put(
                principal.tenant_id,
                campaign_id,
                prepared.json_bytes,
                media_type="application/json",
            )
            markdown_uri = await self._store.put(
                principal.tenant_id,
                campaign_id,
                prepared.markdown_bytes,
                media_type="text/markdown",
            )
            binding = FinalReportBinding(
                principal.tenant_id,
                campaign_id,
                principal.user_id,
                prepared.campaign_status,
                prepared.campaign_version,
                prepared.decision_input_hash,
                prepared.decision_hash,
                prepared.decision.status,
                prepared.automatic_gate_status,
                prepared.manual_review_status,
                prepared.finalization_reason or "final",
                json_uri,
                json_sha,
                markdown_uri,
                markdown_sha,
            )
            try:
                receipt = await self._gateway.bind(binding)
            except InvariantViolation as error:
                if error.code == "report_input_stale" and attempt < self._max_stale_retries:
                    continue
                raise
            return CampaignReportResult(
                campaign_id,
                receipt.gate_status,
                prepared.decision.decision_state,
                prepared.decision_input_hash,
                receipt.decision_hash,
                True,
                prepared.json_bytes,
                prepared.markdown_bytes,
                receipt.json_uri,
                receipt.json_sha256,
                receipt.markdown_uri,
                receipt.markdown_sha256,
                receipt.duplicate,
            )
        raise AssertionError("unreachable")

    async def _read_existing(self, snapshot) -> CampaignReportResult:
        binding = snapshot.existing_final_binding
        assert binding is not None
        required = {
            "gate_status",
            "decision_input_hash",
            "decision_hash",
            "json_uri",
            "json_sha256",
            "markdown_uri",
            "markdown_sha256",
        }
        if set(binding) != required or any(binding[key] is None for key in required):
            raise InvariantViolation(
                "Campaign final report binding is incomplete",
                code="report_binding_corrupt",
            )
        json_bytes = await self._store.get(
            snapshot.tenant_id,
            snapshot.campaign_id,
            str(binding["json_uri"]),
        )
        markdown_bytes = await self._store.get(
            snapshot.tenant_id,
            snapshot.campaign_id,
            str(binding["markdown_uri"]),
        )
        if (
            hashlib.sha256(json_bytes).hexdigest() != binding["json_sha256"]
            or hashlib.sha256(markdown_bytes).hexdigest()
            != binding["markdown_sha256"]
            or hashlib.sha256(json_bytes).hexdigest() != binding["decision_hash"]
        ):
            raise InvariantViolation(
                "Campaign final report artifact failed binding verification",
                code="report_binding_corrupt",
            )
        ReportPrivacyGuard.validate_json_bytes(json_bytes)
        ReportPrivacyGuard.validate_text_bytes(markdown_bytes)
        try:
            decision_state = str(json.loads(json_bytes)["decision"]["state"])
        except (KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as error:
            raise InvariantViolation(
                "Campaign final JSON has an invalid decision schema",
                code="report_binding_corrupt",
            ) from error
        return CampaignReportResult(
            snapshot.campaign_id,
            GateStatus(str(binding["gate_status"])),
            decision_state,
            str(binding["decision_input_hash"]),
            str(binding["decision_hash"]),
            True,
            json_bytes,
            markdown_bytes,
            str(binding["json_uri"]),
            str(binding["json_sha256"]),
            str(binding["markdown_uri"]),
            str(binding["markdown_sha256"]),
            True,
        )


__all__ = ["CampaignReportResult", "ShadowReportService"]
