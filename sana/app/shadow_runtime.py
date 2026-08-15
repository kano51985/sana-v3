"""Production composition root for the transient Shadow Campaign Runner."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import socket
from typing import Any

from pydantic_settings import SettingsConfigDict

from sana.app.settings import SanaSettings
from sana.app.shadow_collector import ShadowCollectorService
from sana.app.shadow_provenance import parse_shadow_attestation_bytes
from sana.app.shadow_report import ShadowReportService
from sana.app.shadow_review import ShadowReviewService
from sana.app.shadow_runner import InteractiveShadowReview, ShadowCampaignRunner
from sana.modules.identity.domain import Principal
from sana.modules.shadow_campaign.budget import CampaignBudgetService
from sana.modules.shadow_campaign.execution import CampaignExecutionService
from sana.modules.shadow_campaign.policy import (
    CampaignPolicyCatalog,
    CostRate,
    ReviewRubric,
)
from sana.modules.shadow_campaign.scheduler import CampaignSchedulingService
from sana.modules.shadow_campaign.service import (
    CampaignProvenance,
    CampaignLifecycleService,
    CampaignService,
    CreateCampaignCommand,
)
from sana.modules.shared.clock import SystemClock
from sana.modules.shared.ids import RandomIdFactory
from sana.platform.db.session import create_database_engine, create_session_factory
from sana.platform.db.shadow_collector import SqlShadowSnapshotReader
from sana.platform.db.shadow_report import SqlShadowReportGateway
from sana.platform.db.shadow_review import SqlShadowReviewProjectionReader
from sana.platform.db.uow import TenantUnitOfWorkFactory
from sana.platform.storage.campaign_reports import LocalCampaignReportStore


class ShadowRuntimeSettings(SanaSettings):
    model_config = SettingsConfigDict(
        env_prefix="SANA_",
        env_file=None,
        extra="ignore",
    )

    shadow_attestation_path: str = "/run/sana/attestation.json"
    campaign_report_root: str = "/var/lib/sana/campaign-reports"
    shadow_review_rubric_path: str = "evals/shadow/review-rubric-v1.json"
    shadow_cost_rate_path: str = "evals/shadow/cost-rates-v1.json"


def _load_json(path: str | Path, maximum: int = 1_000_000) -> dict[str, Any]:
    resolved = Path(path)
    payload = resolved.read_bytes()
    if not payload or len(payload) > maximum:
        raise ValueError(f"Evaluation asset size is invalid: {resolved.name}")
    def strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"Evaluation asset contains duplicate key: {key}")
            result[key] = item
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"Evaluation asset contains non-finite value: {value}")

    value = json.loads(
        payload,
        parse_float=Decimal,
        parse_constant=reject_constant,
        object_pairs_hook=strict_object,
    )
    if not isinstance(value, dict):
        raise ValueError(f"Evaluation asset must be an object: {resolved.name}")
    return value


def load_review_rubric(path: str | Path) -> ReviewRubric:
    value = _load_json(path)
    if set(value) != {"version", "criteria"} or not isinstance(
        value["criteria"],
        list,
    ):
        raise ValueError("Review rubric asset schema is invalid")
    return ReviewRubric(str(value["version"]), tuple(map(str, value["criteria"])))


def load_cost_rate(path: str | Path) -> CostRate:
    value = _load_json(path)
    if set(value) != {
        "version",
        "prompt_per_million_usd",
        "completion_per_million_usd",
        "possibly_billed_run_reserve_usd",
    }:
        raise ValueError("Cost rate asset schema is invalid")
    return CostRate(
        str(value["version"]),
        Decimal(str(value["prompt_per_million_usd"])),
        Decimal(str(value["completion_per_million_usd"])),
        Decimal(str(value["possibly_billed_run_reserve_usd"])),
    )


@dataclass(slots=True)
class ShadowRuntimeBindings:
    runner: ShadowCampaignRunner
    review_coordinator: InteractiveShadowReview
    rubric: ReviewRubric
    cost_rate: CostRate
    provenance: CampaignProvenance
    clock: SystemClock

    def create_command(self, principal: Principal, args, manifest) -> CreateCampaignCommand:
        now = self.clock.now()
        return CreateCampaignCommand(
            principal.tenant_id,
            principal.user_id,
            args.name,
            args.campaign_key,
            args.profile,
            manifest,
            self.rubric,
            self.cost_rate,
            self.provenance,
            now + timedelta(days=365),
            args.parent_smoke_campaign_id,
        )

    async def review(self, principal: Principal, campaign_id):
        return await self.review_coordinator.review(principal, campaign_id)


@asynccontextmanager
async def shadow_runtime(principal, api, args):
    del principal, args
    settings = ShadowRuntimeSettings()
    attestation = parse_shadow_attestation_bytes(
        Path(settings.shadow_attestation_path).read_bytes()
    )
    rubric = load_review_rubric(settings.shadow_review_rubric_path)
    cost_rate = load_cost_rate(settings.shadow_cost_rate_path)
    catalog = CampaignPolicyCatalog.standard(
        review_rubrics=(rubric,),
        cost_rates=(cost_rate,),
    )
    engine = create_database_engine(settings.database_url)
    sessions = create_session_factory(engine)
    uow_factory = TenantUnitOfWorkFactory(sessions)
    clock = SystemClock()
    snapshot_reader = SqlShadowSnapshotReader(sessions)
    campaigns = CampaignService(
        uow_factory,
        RandomIdFactory(),
        clock,
        catalog,
    )
    lifecycle = CampaignLifecycleService(uow_factory, clock)
    scheduling = CampaignSchedulingService(uow_factory, clock, catalog)
    execution = CampaignExecutionService(uow_factory, api)
    collector = ShadowCollectorService(uow_factory, snapshot_reader)
    reports = ShadowReportService(
        SqlShadowReportGateway(sessions, snapshot_reader),
        LocalCampaignReportStore(settings.campaign_report_root),
    )
    runner = ShadowCampaignRunner(
        uow_factory=uow_factory,
        campaigns=campaigns,
        lifecycle=lifecycle,
        scheduling=scheduling,
        budget=CampaignBudgetService(uow_factory),
        execution=execution,
        collector=collector,
        reports=reports,
        clock=clock,
        worker_id=f"{socket.gethostname()}:{os.getpid()}",
    )
    review = ShadowReviewService(
        uow_factory,
        SqlShadowReviewProjectionReader(sessions),
    )
    try:
        yield ShadowRuntimeBindings(
            runner,
            InteractiveShadowReview(runner, review),
            rubric,
            cost_rate,
            attestation.provenance,
            clock,
        )
    finally:
        await engine.dispose()


__all__ = [
    "ShadowRuntimeBindings",
    "ShadowRuntimeSettings",
    "load_cost_rate",
    "load_review_rubric",
    "shadow_runtime",
]
