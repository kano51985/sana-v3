"""Versioned profiles and immutable release-gate policy snapshots."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

from sana.modules.shadow_campaign.domain import canonical_snapshot, snapshot_hash
from sana.modules.shared.errors import InvariantViolation


class GateKind(StrEnum):
    SMOKE = "SMOKE"
    FULL = "FULL"


@dataclass(frozen=True, slots=True)
class CampaignProfile:
    version: str
    max_runs: int
    repetitions: int
    max_concurrency: int
    provider_call_admission_ceiling: int
    provider_call_structural_ceiling: int
    estimated_cost_stop_threshold: Decimal
    gate_policy_version: str
    smoke_only: bool

    def __post_init__(self) -> None:
        if not self.version.strip() or not self.gate_policy_version.strip():
            raise ValueError("Profile and gate policy versions cannot be empty")
        if self.max_runs < 1 or self.repetitions < 1:
            raise ValueError("Profile run limits must be positive")
        if self.max_runs % self.repetitions:
            raise ValueError("max_runs must be divisible by repetitions")
        if not 1 <= self.max_concurrency <= 2:
            raise ValueError("Campaign concurrency must be between one and two")
        expected_structural = self.max_runs * 8
        if self.provider_call_structural_ceiling != expected_structural:
            raise ValueError(
                "Provider-call structural ceiling must equal max_runs times eight"
            )
        if not 0 < self.provider_call_admission_ceiling <= expected_structural:
            raise ValueError("Provider-call admission ceiling is invalid")
        if (
            not self.estimated_cost_stop_threshold.is_finite()
            or self.estimated_cost_stop_threshold <= 0
        ):
            raise ValueError("Estimated cost stop threshold must be positive")

    def snapshot(self) -> dict[str, object]:
        return canonical_snapshot(self)

    @property
    def sha256(self) -> str:
        return snapshot_hash(self.snapshot())


@dataclass(frozen=True, slots=True)
class GatePolicy:
    version: str
    kind: GateKind
    min_terminal_results: int
    min_actual_fast_results: int
    min_actual_research_results: int
    min_distinct_cases: int
    min_unanswerable_cases: int
    min_unanswerable_results: int
    required_reviews: int
    required_gold_cases: int
    fast_latency_p95_ms: int
    research_latency_p95_ms: int
    min_mode_accuracy_bps: int
    min_coverage_macro_bps: int
    min_coverage_stratum_bps: int
    min_gold_pass_bps: int
    min_review_correct_bps: int
    min_review_citation_bps: int
    min_review_source_bps: int
    min_review_freshness_bps: int
    min_review_completeness_bps: int
    min_unanswerable_gap_bps: int
    max_degraded_bps: int
    max_infrastructure_failure_bps: int
    max_projected_full_cost_usd: Decimal | None = None
    require_every_run_within_deadline: bool = False
    require_exact_sample_counts: bool = False

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("Gate policy version cannot be empty")
        count_values = (
            self.min_terminal_results,
            self.min_actual_fast_results,
            self.min_actual_research_results,
            self.min_distinct_cases,
            self.min_unanswerable_cases,
            self.min_unanswerable_results,
            self.required_reviews,
            self.required_gold_cases,
        )
        if any(value < 0 for value in count_values):
            raise ValueError("Gate sample counts cannot be negative")
        if self.fast_latency_p95_ms <= 0 or self.research_latency_p95_ms <= 0:
            raise ValueError("Gate latency thresholds must be positive")
        basis_points = (
            self.min_mode_accuracy_bps,
            self.min_coverage_macro_bps,
            self.min_coverage_stratum_bps,
            self.min_gold_pass_bps,
            self.min_review_correct_bps,
            self.min_review_citation_bps,
            self.min_review_source_bps,
            self.min_review_freshness_bps,
            self.min_review_completeness_bps,
            self.min_unanswerable_gap_bps,
            self.max_degraded_bps,
            self.max_infrastructure_failure_bps,
        )
        if any(not 0 <= value <= 10_000 for value in basis_points):
            raise ValueError("Gate ratios must use basis points between zero and 10000")
        if self.max_projected_full_cost_usd is not None and self.max_projected_full_cost_usd <= 0:
            raise ValueError("Projected cost threshold must be positive")

    @property
    def operational_only(self) -> bool:
        return self.kind is GateKind.SMOKE

    def snapshot(self) -> dict[str, object]:
        return canonical_snapshot(self)

    @property
    def sha256(self) -> str:
        return snapshot_hash(self)


@dataclass(frozen=True, slots=True)
class CostRate:
    version: str
    prompt_per_million_usd: Decimal
    completion_per_million_usd: Decimal
    possibly_billed_run_reserve_usd: Decimal

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("Cost rate version cannot be empty")
        if any(
            not value.is_finite() or value < 0
            for value in (
                self.prompt_per_million_usd,
                self.completion_per_million_usd,
                self.possibly_billed_run_reserve_usd,
            )
        ):
            raise ValueError("Cost rates cannot be negative")

    @property
    def sha256(self) -> str:
        return snapshot_hash(self)

    def snapshot(self) -> dict[str, object]:
        return canonical_snapshot(self)


@dataclass(frozen=True, slots=True)
class ReviewRubric:
    version: str
    criteria: tuple[str, ...] = (
        "correctness",
        "citation_relevance",
        "source_appropriateness",
        "freshness",
        "completeness",
    )

    def __post_init__(self) -> None:
        if not self.version.strip() or not self.criteria:
            raise ValueError("Review rubric requires a version and criteria")
        if len(set(self.criteria)) != len(self.criteria) or any(
            not item.strip() for item in self.criteria
        ):
            raise ValueError("Review rubric criteria must be unique and non-empty")

    @property
    def sha256(self) -> str:
        return snapshot_hash(self)

    def snapshot(self) -> dict[str, object]:
        return canonical_snapshot(self)


class CampaignPolicyCatalog:
    """Composition-owned allowlist preventing request-defined release thresholds."""

    def __init__(
        self,
        profiles: Iterable[CampaignProfile],
        policies: Iterable[GatePolicy],
        review_rubrics: Iterable[ReviewRubric],
        cost_rates: Iterable[CostRate],
    ) -> None:
        profile_items = tuple(profiles)
        policy_items = tuple(policies)
        rubric_items = tuple(review_rubrics)
        rate_items = tuple(cost_rates)
        profile_map = {item.version: item for item in profile_items}
        policy_map = {item.version: item for item in policy_items}
        rubric_map = {item.version: item for item in rubric_items}
        rate_map = {item.version: item for item in rate_items}
        if not profile_map or not policy_map or not rubric_map or not rate_map:
            raise ValueError("Campaign policy catalog cannot be empty")
        if any(
            actual != expected
            for actual, expected in (
                (len(profile_map), len(profile_items)),
                (len(policy_map), len(policy_items)),
                (len(rubric_map), len(rubric_items)),
                (len(rate_map), len(rate_items)),
            )
        ):
            raise ValueError("Campaign policy catalog versions must be unique")
        for profile in profile_map.values():
            if profile.gate_policy_version not in policy_map:
                raise ValueError(
                    f"Profile {profile.version} references an unknown gate policy"
                )
        self._profiles = MappingProxyType(profile_map)
        self._policies = MappingProxyType(policy_map)
        self._review_rubrics = MappingProxyType(rubric_map)
        self._cost_rates = MappingProxyType(rate_map)

    @classmethod
    def standard(
        cls,
        *,
        review_rubrics: Iterable[ReviewRubric],
        cost_rates: Iterable[CostRate],
    ) -> "CampaignPolicyCatalog":
        return cls(
            (DOCKER_SMOKE_V1, SHADOW_FULL_V1),
            (SHADOW_SMOKE_GATE_V1, SHADOW_FULL_GATE_V2),
            review_rubrics,
            cost_rates,
        )

    def resolve(self, profile_version: str) -> tuple[CampaignProfile, GatePolicy]:
        profile = self._profiles.get(profile_version)
        if profile is None:
            raise InvariantViolation(
                "Campaign profile is not in the locked policy catalog",
                code="unknown_campaign_profile",
                details={"profile_version": profile_version},
            )
        return profile, self._policies[profile.gate_policy_version]

    def resolve_evaluation_assets(
        self,
        review_rubric: ReviewRubric,
        cost_rate: CostRate,
    ) -> tuple[ReviewRubric, CostRate]:
        registered_rubric = self._review_rubrics.get(review_rubric.version)
        registered_rate = self._cost_rates.get(cost_rate.version)
        if (
            registered_rubric is None
            or registered_rate is None
            or registered_rubric.sha256 != review_rubric.sha256
            or registered_rate.sha256 != cost_rate.sha256
        ):
            raise InvariantViolation(
                "Review rubric or cost rate is not in the locked policy catalog",
                code="unapproved_evaluation_asset",
            )
        return registered_rubric, registered_rate


DOCKER_SMOKE_V1 = CampaignProfile(
    version="docker-smoke-v1",
    max_runs=6,
    repetitions=1,
    max_concurrency=2,
    provider_call_admission_ceiling=32,
    provider_call_structural_ceiling=48,
    estimated_cost_stop_threshold=Decimal("0.01"),
    gate_policy_version="shadow-smoke-gate-v1",
    smoke_only=True,
)

SHADOW_FULL_V1 = CampaignProfile(
    version="shadow-full-v1",
    max_runs=120,
    repetitions=3,
    max_concurrency=2,
    provider_call_admission_ceiling=480,
    provider_call_structural_ceiling=960,
    estimated_cost_stop_threshold=Decimal("0.10"),
    gate_policy_version="shadow-gate-v2",
    smoke_only=False,
)

SHADOW_SMOKE_GATE_V1 = GatePolicy(
    version="shadow-smoke-gate-v1",
    kind=GateKind.SMOKE,
    min_terminal_results=6,
    min_actual_fast_results=3,
    min_actual_research_results=3,
    min_distinct_cases=6,
    min_unanswerable_cases=1,
    min_unanswerable_results=1,
    required_reviews=0,
    required_gold_cases=0,
    fast_latency_p95_ms=15_000,
    research_latency_p95_ms=120_000,
    min_mode_accuracy_bps=0,
    min_coverage_macro_bps=0,
    min_coverage_stratum_bps=0,
    min_gold_pass_bps=0,
    min_review_correct_bps=0,
    min_review_citation_bps=0,
    min_review_source_bps=0,
    min_review_freshness_bps=0,
    min_review_completeness_bps=0,
    min_unanswerable_gap_bps=0,
    max_degraded_bps=10_000,
    max_infrastructure_failure_bps=0,
    max_projected_full_cost_usd=Decimal("0.10"),
    require_every_run_within_deadline=True,
    require_exact_sample_counts=True,
)

SHADOW_FULL_GATE_V2 = GatePolicy(
    version="shadow-gate-v2",
    kind=GateKind.FULL,
    min_terminal_results=100,
    min_actual_fast_results=50,
    min_actual_research_results=50,
    min_distinct_cases=40,
    min_unanswerable_cases=8,
    min_unanswerable_results=20,
    required_reviews=20,
    required_gold_cases=16,
    fast_latency_p95_ms=15_000,
    research_latency_p95_ms=120_000,
    min_mode_accuracy_bps=9_500,
    min_coverage_macro_bps=8_000,
    min_coverage_stratum_bps=7_000,
    min_gold_pass_bps=9_500,
    min_review_correct_bps=9_000,
    min_review_citation_bps=9_500,
    min_review_source_bps=9_500,
    min_review_freshness_bps=9_500,
    min_review_completeness_bps=9_000,
    min_unanswerable_gap_bps=10_000,
    max_degraded_bps=1_000,
    max_infrastructure_failure_bps=100,
)
