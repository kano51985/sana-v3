from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from sana.app.search_operations import (
    SearchStepOperations,
    _bounded_fetch_deadline,
    _select_ranked_hits,
)
from sana.modules.evidence.source_authority import SourceAuthorityPolicy
from sana.modules.orchestration.domain import ArtifactRef, SearchMode, StepType
from sana.modules.orchestration.step_handlers import StepExecutionContext
from sana.modules.shared.clock import FrozenClock
from sana.modules.shared.ids import TraceContext


NOW = datetime(2026, 8, 15, tzinfo=timezone.utc)


class MemoryArtifacts:
    def __init__(self, payloads: dict[str, dict]) -> None:
        self.payloads = payloads
        self.last_payload: dict | None = None

    async def get_json(self, tenant_id, reference):
        del tenant_id
        return self.payloads[reference.uri]

    async def put_json(self, tenant_id, run_id, payload):
        del tenant_id, run_id
        self.last_payload = payload
        return ArtifactRef("artifact://answer", "f" * 64)


class NeverCancelled:
    async def is_cancelled(self, tenant_id, run_id):
        del tenant_id, run_id
        return False


def _hit(url: str, *, score: float, rank: int) -> dict:
    return {
        "canonical_url": url,
        "score": score,
        "rank": rank,
    }


def test_fast_selection_prioritizes_official_and_diverse_sources() -> None:
    selected = _select_ranked_hits(
        (
            _hit("https://blog.example.com/python", score=1.0, rank=1),
            _hit("https://www.python.org/downloads/", score=0.25, rank=4),
            _hit("https://docs.python.org/3/", score=0.5, rank=2),
            _hit("https://independent.example.net/python", score=0.4, rank=3),
        ),
        authority_policy=SourceAuthorityPolicy(),
        entity="Python",
        mode=SearchMode.FAST,
        max_selected_hits=4,
    )

    assert len(selected) == 1
    assert selected[0]["canonical_url"] == "https://docs.python.org/3/"


def test_fetch_deadline_is_bounded_per_source_and_by_step() -> None:
    assert _bounded_fetch_deadline(
        SearchMode.FAST,
        NOW,
        NOW + timedelta(seconds=30),
    ) == NOW + timedelta(seconds=6)
    assert _bounded_fetch_deadline(
        SearchMode.RESEARCH,
        NOW,
        NOW + timedelta(seconds=5),
    ) == NOW + timedelta(seconds=5)


def test_fast_selection_keeps_two_reviewed_official_failovers() -> None:
    candidates = []
    for url, score in (
        (
            "https://www.iana.org/assignments/http-status-codes/"
            "http-status-codes-1.csv",
            1.0,
        ),
        (
            "https://www.rfc-editor.org/rfc/rfc9110.html#name-404-not-found",
            0.95,
        ),
    ):
        candidate = _hit(url, score=score, rank=1)
        candidate["provider"] = "direct"
        candidates.append(candidate)

    selected = _select_ranked_hits(
        tuple(candidates),
        authority_policy=SourceAuthorityPolicy(),
        entity="HTTP 404",
        mode=SearchMode.FAST,
        max_selected_hits=4,
    )

    assert len(selected) == 2
    assert {item["canonical_url"] for item in selected} == {
        "https://www.iana.org/assignments/http-status-codes/"
        "http-status-codes-1.csv",
        "https://www.rfc-editor.org/rfc/rfc9110.html#name-404-not-found",
    }


def test_fast_selection_prefers_reviewed_direct_page_over_search_homepage() -> None:
    homepage = _hit("https://nodejs.org/", score=1.0, rank=1)
    homepage["provider"] = "bing_rss"
    reviewed = _hit(
        "https://nodejs.org/en/about/previous-releases",
        score=1.0,
        rank=1,
    )
    reviewed["provider"] = "direct"

    selected = _select_ranked_hits(
        (homepage, reviewed),
        authority_policy=SourceAuthorityPolicy(),
        entity="Node.js",
        mode=SearchMode.FAST,
        max_selected_hits=4,
    )

    assert selected[0]["canonical_url"] == reviewed["canonical_url"]


def test_fast_selection_keeps_two_reviewed_pages_on_one_official_domain() -> None:
    creator = _hit(
        "https://www.python.org/download/releases/2.1/license/",
        score=1.0,
        rank=1,
    )
    creator["provider"] = "direct"
    history = _hit(
        "https://docs.python.org/3/license.html#history-of-the-software",
        score=1.0,
        rank=1,
    )
    history["provider"] = "direct"

    selected = _select_ranked_hits(
        (creator, history),
        authority_policy=SourceAuthorityPolicy(),
        entity="Python",
        mode=SearchMode.FAST,
        max_selected_hits=4,
    )

    assert len(selected) == 2


def test_research_selection_covers_facts_before_redundant_publishers() -> None:
    version, changes, patch, team = (str(uuid4()) for _ in range(4))
    candidates = []
    for url, fact_id, score in (
        (
            "https://www.ea.com/games/apex-legends/apex-legends/news/"
            "overclocked-patch-notes",
            version,
            0.8,
        ),
        (
            "https://www.ea.com/games/apex-legends/apex-legends/news/"
            "breach-patch-notes",
            changes,
            0.7,
        ),
        (
            "https://www.ea.com/games/apex-legends/apex-legends/news/"
            "overclocked-midseason-patch-notes",
            patch,
            0.6,
        ),
        ("https://analysis.example.net/apex-team", team, 0.5),
        ("https://duplicate.example.org/apex-version", version, 1.0),
    ):
        candidate = _hit(url, score=score, rank=1)
        candidate["fact_ids"] = [fact_id]
        candidate["provider"] = (
            "direct" if "ea.com" in url else "bing_rss"
        )
        candidates.append(candidate)
    generic = _hit("https://generic.example.com/apex", score=1.0, rank=1)
    generic["fact_ids"] = [version, changes, patch, team]
    generic["provider"] = "bing_rss"
    candidates.append(generic)
    generic_official = _hit(
        "https://www.ea.com/games/apex-legends/apex-legends",
        score=1.0,
        rank=1,
    )
    generic_official["fact_ids"] = [version, changes, patch, team]
    generic_official["provider"] = "bing_rss"
    candidates.append(generic_official)

    selected = _select_ranked_hits(
        tuple(candidates),
        authority_policy=SourceAuthorityPolicy(),
        entity="Apex Legends",
        mode=SearchMode.RESEARCH,
        max_selected_hits=4,
    )

    assert len(selected) == 4
    assert {
        fact_id
        for item in selected
        for fact_id in item["fact_ids"]
    } == {version, changes, patch, team}
    assert sum("ea.com" in item["canonical_url"] for item in selected) == 3
    assert any(
        item["canonical_url"] == "https://analysis.example.net/apex-team"
        for item in selected
    )
    assert all(
        item["canonical_url"]
        not in {
            "https://generic.example.com/apex",
            "https://www.ea.com/games/apex-legends/apex-legends",
        }
        for item in selected
    )


@pytest.mark.asyncio
async def test_selection_preserves_all_fact_bindings_for_one_canonical_url() -> None:
    tenant_id, run_id = uuid4(), uuid4()
    first_fact, second_fact = uuid4(), uuid4()
    plan_ref = ArtifactRef("artifact://plan", "1" * 64)
    first_ref = ArtifactRef("artifact://discovery-1", "2" * 64)
    second_ref = ArtifactRef("artifact://discovery-2", "3" * 64)
    input_ref = ArtifactRef("artifact://selection-input", "4" * 64)
    shared_url = "https://git-scm.com/book/en/v2/Git-Internals-Git-Objects"

    def discovery(fact_id, hit_id):
        return {
            "responses": [
                {
                    "error": None,
                    "hits": [
                        {
                            "id": str(hit_id),
                            "fact_id": str(fact_id),
                            "canonical_url": shared_url,
                            "score": 1.0,
                            "rank": 1,
                        }
                    ],
                }
            ]
        }

    artifacts = MemoryArtifacts(
        {
            input_ref.uri: {
                "plan_ref": {"uri": plan_ref.uri, "sha256": plan_ref.sha256},
                "discovery_refs": [
                    {"uri": first_ref.uri, "sha256": first_ref.sha256},
                    {"uri": second_ref.uri, "sha256": second_ref.sha256},
                ],
            },
            plan_ref.uri: {"mode": "RESEARCH", "intent": {"entity": "Git"}},
            first_ref.uri: discovery(first_fact, uuid4()),
            second_ref.uri: discovery(second_fact, uuid4()),
        }
    )
    operations = SearchStepOperations(
        uow_factory=None,  # type: ignore[arg-type]
        artifacts=artifacts,  # type: ignore[arg-type]
        planner=None,  # type: ignore[arg-type]
        discovery=None,  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
        provider_names=("direct",),
    )
    context = StepExecutionContext(
        tenant_id=tenant_id,
        run_id=run_id,
        step_id=uuid4(),
        step_key="select",
        step_type=StepType.SELECT,
        attempt_id=uuid4(),
        attempt_no=1,
        trace_context=TraceContext.create(),
        deadline_at=NOW + timedelta(seconds=5),
        input_ref=input_ref,
        cancellation=NeverCancelled(),
        clock=FrozenClock(NOW),
    )

    await operations.select(context)

    assert artifacts.last_payload is not None
    assert len(artifacts.last_payload["selected"]) == 1
    assert set(artifacts.last_payload["selected"][0]["fact_ids"]) == {
        str(first_fact),
        str(second_fact),
    }


@pytest.mark.asyncio
async def test_synthesis_reports_pipeline_deadline_degradation() -> None:
    tenant_id = uuid4()
    run_id = uuid4()
    fact_id = uuid4()
    plan_ref = ArtifactRef("artifact://plan", "a" * 64)
    input_ref = ArtifactRef("artifact://input", "b" * 64)
    artifacts = MemoryArtifacts(
        {
            plan_ref.uri: {
                "schema": "sana.plan.v1",
                "degraded": False,
                "intent": {"locale": "zh-CN"},
                "facts": [
                    {
                        "id": str(fact_id),
                        "key": "python_stable_version",
                        "fact_type": "version",
                        "description": "Python 当前稳定版本",
                        "subject": "Python",
                        "required": True,
                        "freshness": "CURRENT",
                        "consequence": "HIGH",
                        "preferred_source_kinds": ["official"],
                    }
                ],
            },
            input_ref.uri: {
                "schema": "sana.synthesis-input.v2",
                "plan_ref": {"uri": plan_ref.uri, "sha256": plan_ref.sha256},
                "verify_ref": None,
                "provider_failures": 0,
                "pipeline_degradation_codes": [
                    "phase_deadline_exceeded",
                    "verify_failed",
                ],
            },
        }
    )
    operations = SearchStepOperations(
        uow_factory=None,  # type: ignore[arg-type]
        artifacts=artifacts,  # type: ignore[arg-type]
        planner=None,  # type: ignore[arg-type]
        discovery=None,  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
        provider_names=("direct",),
    )
    context = StepExecutionContext(
        tenant_id=tenant_id,
        run_id=run_id,
        step_id=uuid4(),
        step_key="synthesize",
        step_type=StepType.SYNTHESIZE,
        attempt_id=uuid4(),
        attempt_no=1,
        trace_context=TraceContext.create(),
        deadline_at=NOW + timedelta(seconds=5),
        input_ref=input_ref,
        cancellation=NeverCancelled(),
        clock=FrozenClock(NOW),
    )

    await operations.synthesize(context)

    assert artifacts.last_payload is not None
    assert artifacts.last_payload["degraded"] is True
    assert artifacts.last_payload["stop_reason"] == "TIME_BUDGET"
    assert artifacts.last_payload["degradation_codes"] == [
        "phase_deadline_exceeded",
        "verify_failed",
    ]
    assert all(
        "kind" in claim and "fact_id" in claim
        for claim in artifacts.last_payload["claims"]
    )

    artifacts.payloads[plan_ref.uri]["degraded"] = True
    artifacts.payloads[input_ref.uri]["pipeline_degradation_codes"] = []
    artifacts.last_payload = None

    await operations.synthesize(context)

    assert artifacts.last_payload is not None
    assert artifacts.last_payload["degraded"] is True
    assert artifacts.last_payload["stop_reason"] == "PROVIDER_FAILURE"
