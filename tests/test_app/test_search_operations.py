import hashlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from sana.app.search_operations import (
    SearchStepOperations,
    _bounded_fetch_deadline,
    _select_ranked_hits,
)
from sana.modules.content.domain import (
    DocumentReusePolicy,
    FetchArtifact,
    FetchStatus,
    ReusableContentSnapshot,
)
from sana.modules.evidence.source_authority import SourceAuthorityPolicy
from sana.modules.orchestration.domain import ArtifactRef, SearchMode, StepType
from sana.modules.orchestration.step_handlers import StepExecutionContext
from sana.modules.shared.clock import FrozenClock
from sana.modules.shared.errors import ErrorCategory, TypedError
from sana.modules.shared.ids import TraceContext


NOW = datetime(2026, 8, 15, tzinfo=timezone.utc)


class MemoryArtifacts:
    def __init__(self, payloads: dict[str, dict]) -> None:
        self.payloads = payloads
        self.byte_payloads: dict[str, bytes] = {}
        self.last_payload: dict | None = None
        self.last_bytes_ref: ArtifactRef | None = None

    async def get_json(self, tenant_id, reference):
        del tenant_id
        return self.payloads[reference.uri]

    async def put_json(self, tenant_id, run_id, payload):
        del tenant_id, run_id
        self.last_payload = payload
        return ArtifactRef("artifact://answer", "f" * 64)

    async def get_bytes(self, tenant_id, reference):
        del tenant_id
        return self.byte_payloads[reference.uri]

    async def put_bytes(self, tenant_id, run_id, payload):
        digest = hashlib.sha256(payload).hexdigest()
        reference = ArtifactRef(
            f"artifact://{tenant_id}/{run_id}/{digest}",
            digest,
        )
        self.byte_payloads[reference.uri] = payload
        self.last_bytes_ref = reference
        return reference


class NeverCancelled:
    async def is_cancelled(self, tenant_id, run_id):
        del tenant_id, run_id
        return False


class StubFetcher:
    def __init__(self, artifact: FetchArtifact) -> None:
        self.artifact = artifact
        self.calls = 0

    async def fetch(self, request):
        self.calls += 1
        return self.artifact


class StubSnapshotReader:
    def __init__(self, snapshot: ReusableContentSnapshot | None) -> None:
        self.snapshot = snapshot
        self.calls: list[tuple[object, str]] = []

    async def latest_for_url(self, tenant_id, canonical_url_hash):
        self.calls.append((tenant_id, canonical_url_hash))
        return self.snapshot


class RecordingURLValidator:
    def __init__(self, error: TypedError | None = None) -> None:
        self.error = error
        self.urls: list[str] = []

    async def validate(self, url: str) -> None:
        self.urls.append(url)
        if self.error is not None:
            raise self.error


def _failed_fetch(url: str, error: TypedError) -> FetchArtifact:
    return FetchArtifact(
        request_url=url,
        final_url=url,
        status=FetchStatus.FAILED,
        http_status=None,
        media_type=None,
        body=b"",
        content_hash=None,
        fetched_at=NOW,
        error=error,
    )


def _successful_fetch(url: str, body: bytes) -> FetchArtifact:
    return FetchArtifact(
        request_url=url,
        final_url=url,
        status=FetchStatus.SUCCEEDED,
        http_status=200,
        media_type="text/html",
        body=body,
        content_hash=hashlib.sha256(body).hexdigest(),
        fetched_at=NOW,
    )


def _fetch_case(*, source_age: timedelta, freshness: str = "STABLE"):
    tenant_id, run_id, source_run_id, fact_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    url = "https://docs.example.test/source"
    redirect = "https://www.example.test/final"
    body = b"<html><body>stable source</body></html>"
    digest = hashlib.sha256(body).hexdigest()
    plan_ref = ArtifactRef("artifact://plan", "a" * 64)
    input_ref = ArtifactRef("artifact://fetch-input", "b" * 64)
    source_ref = ArtifactRef(
        f"artifact://{tenant_id}/{source_run_id}/{digest}",
        digest,
    )
    artifacts = MemoryArtifacts(
        {
            plan_ref.uri: {
                "mode": "FAST",
                "facts": [
                    {
                        "id": str(fact_id),
                        "freshness": freshness,
                    }
                ],
            },
            input_ref.uri: {
                "plan_ref": {
                    "uri": plan_ref.uri,
                    "sha256": plan_ref.sha256,
                },
                "hit": {
                    "id": str(uuid4()),
                    "canonical_url": url,
                    "fact_ids": [str(fact_id)],
                },
            },
        }
    )
    artifacts.byte_payloads[source_ref.uri] = body
    snapshot = ReusableContentSnapshot(
        source_fetch_artifact_id=uuid4(),
        source_run_id=source_run_id,
        source_document_version_id=uuid4(),
        request_url=url,
        final_url=redirect,
        http_status=200,
        media_type="text/html",
        content_hash=digest,
        storage_uri=source_ref.uri,
        fetched_at=NOW - source_age,
        redirects=(redirect,),
    )
    context = StepExecutionContext(
        tenant_id=tenant_id,
        run_id=run_id,
        step_id=uuid4(),
        step_key="fetch:1:test",
        step_type=StepType.FETCH,
        attempt_id=uuid4(),
        attempt_no=1,
        trace_context=TraceContext.create(),
        deadline_at=NOW + timedelta(seconds=10),
        input_ref=input_ref,
        cancellation=NeverCancelled(),
        clock=FrozenClock(NOW),
    )
    return artifacts, snapshot, context, url, redirect, body


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
    creator_fact, history_fact = str(uuid4()), str(uuid4())
    creator = _hit(
        "https://www.python.org/download/releases/2.1/license/",
        score=1.0,
        rank=1,
    )
    creator["provider"] = "direct"
    creator["fact_ids"] = [creator_fact]
    history = _hit(
        "https://docs.python.org/3/license.html#history-of-the-software",
        score=1.0,
        rank=1,
    )
    history["provider"] = "direct"
    history["fact_ids"] = [history_fact]
    independent = _hit(
        "https://independent.example.net/python-history",
        score=1.0,
        rank=1,
    )
    independent["provider"] = "bing_rss"
    independent["fact_ids"] = [creator_fact, history_fact]

    selected = _select_ranked_hits(
        (creator, history, independent),
        authority_policy=SourceAuthorityPolicy(),
        entity="Python",
        mode=SearchMode.FAST,
        max_selected_hits=4,
    )

    assert len(selected) == 2
    assert {item["canonical_url"] for item in selected} == {
        creator["canonical_url"],
        history["canonical_url"],
    }


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


@pytest.mark.asyncio
async def test_fresh_document_reuse_skips_network_and_preserves_fetch_time() -> None:
    artifacts, snapshot, context, url, redirect, body = _fetch_case(
        source_age=timedelta(hours=1)
    )
    fetcher = StubFetcher(_successful_fetch(url, b"unexpected live body"))
    reader = StubSnapshotReader(snapshot)
    validator = RecordingURLValidator()
    operations = SearchStepOperations(
        uow_factory=None,  # type: ignore[arg-type]
        artifacts=artifacts,  # type: ignore[arg-type]
        planner=None,  # type: ignore[arg-type]
        discovery=None,  # type: ignore[arg-type]
        fetcher=fetcher,
        provider_names=("direct",),
        snapshot_reader=reader,
        url_safety_validator=validator,
        document_reuse_policy=DocumentReusePolicy.default(),
        document_reuse_enabled=True,
    )

    await operations.fetch(context)

    assert fetcher.calls == 0
    assert validator.urls == [url, redirect]
    assert artifacts.last_bytes_ref is not None
    assert artifacts.byte_payloads[artifacts.last_bytes_ref.uri] == body
    assert artifacts.last_payload is not None
    assert artifacts.last_payload["schema"] == "sana.fetch.v2"
    assert artifacts.last_payload["decision"] == "CACHE_FRESH"
    assert artifacts.last_payload["fetcher"] == "document-cache"
    assert artifacts.last_payload["fetched_at"] == snapshot.fetched_at.isoformat()
    assert artifacts.last_payload["degradation_codes"] == []
    metadata = artifacts.last_payload["cache_metadata"]
    assert metadata["policy_version"] == "document-reuse-v1"
    assert metadata["strictest_freshness"] == "STABLE"
    assert metadata["source_run_id"] == str(snapshot.source_run_id)
    assert metadata["source_fetched_at"] == snapshot.fetched_at.isoformat()
    assert metadata["reused_at"] == NOW.isoformat()
    assert metadata["reuse_age_seconds"] == 3600
    assert "live_error_message" not in metadata


@pytest.mark.asyncio
async def test_stale_snapshot_prefers_live_success() -> None:
    artifacts, snapshot, context, url, _, _ = _fetch_case(
        source_age=timedelta(days=2)
    )
    live_body = b"<html><body>new live source</body></html>"
    fetcher = StubFetcher(_successful_fetch(url, live_body))
    operations = SearchStepOperations(
        uow_factory=None,  # type: ignore[arg-type]
        artifacts=artifacts,  # type: ignore[arg-type]
        planner=None,  # type: ignore[arg-type]
        discovery=None,  # type: ignore[arg-type]
        fetcher=fetcher,
        provider_names=("direct",),
        snapshot_reader=StubSnapshotReader(snapshot),
        url_safety_validator=RecordingURLValidator(),
        document_reuse_enabled=True,
    )

    await operations.fetch(context)

    assert fetcher.calls == 1
    assert artifacts.last_payload is not None
    assert artifacts.last_payload["decision"] == "LIVE"
    assert artifacts.last_payload["fetcher"] == "http"
    assert artifacts.last_payload["cache_metadata"] == {}
    assert artifacts.byte_payloads[artifacts.last_bytes_ref.uri] == live_body  # type: ignore[union-attr]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        TypedError(ErrorCategory.TRANSIENT, "fetch_network_failure", "secret host"),
        TypedError(
            ErrorCategory.BUDGET,
            "fetch_deadline_exceeded",
            "secret deadline",
            retryable=False,
        ),
        TypedError(ErrorCategory.TRANSIENT, "fetch_http_429", "secret rate"),
        TypedError(ErrorCategory.TRANSIENT, "fetch_http_503", "secret server"),
    ],
)
async def test_stale_if_error_uses_reviewed_failures_without_error_text(
    error: TypedError,
) -> None:
    artifacts, snapshot, context, url, _, body = _fetch_case(
        source_age=timedelta(days=2)
    )
    operations = SearchStepOperations(
        uow_factory=None,  # type: ignore[arg-type]
        artifacts=artifacts,  # type: ignore[arg-type]
        planner=None,  # type: ignore[arg-type]
        discovery=None,  # type: ignore[arg-type]
        fetcher=StubFetcher(_failed_fetch(url, error)),
        provider_names=("direct",),
        snapshot_reader=StubSnapshotReader(snapshot),
        url_safety_validator=RecordingURLValidator(),
        document_reuse_enabled=True,
    )

    await operations.fetch(context)

    assert artifacts.last_payload is not None
    assert artifacts.last_payload["decision"] == "CACHE_STALE_IF_ERROR"
    assert artifacts.last_payload["degradation_codes"] == [
        "fetch_cache_stale_if_error"
    ]
    assert artifacts.byte_payloads[artifacts.last_bytes_ref.uri] == body  # type: ignore[union-attr]
    metadata = artifacts.last_payload["cache_metadata"]
    assert metadata["live_error_category"] == error.category.value
    assert metadata["live_error_code"] == error.code
    assert "secret" not in repr(metadata)


@pytest.mark.asyncio
async def test_stale_if_error_rejects_noneligible_error_or_expired_current() -> None:
    for source_age, freshness, error in (
        (
            timedelta(days=2),
            "STABLE",
            TypedError(ErrorCategory.CONTENT, "fetch_http_404", "not found"),
        ),
        (
            timedelta(hours=3),
            "CURRENT",
            TypedError(ErrorCategory.TRANSIENT, "fetch_network_failure", "network"),
        ),
    ):
        artifacts, snapshot, context, url, _, _ = _fetch_case(
            source_age=source_age,
            freshness=freshness,
        )
        operations = SearchStepOperations(
            uow_factory=None,  # type: ignore[arg-type]
            artifacts=artifacts,  # type: ignore[arg-type]
            planner=None,  # type: ignore[arg-type]
            discovery=None,  # type: ignore[arg-type]
            fetcher=StubFetcher(_failed_fetch(url, error)),
            provider_names=("direct",),
            snapshot_reader=StubSnapshotReader(snapshot),
            url_safety_validator=RecordingURLValidator(),
            document_reuse_enabled=True,
        )
        with pytest.raises(TypedError) as raised:
            await operations.fetch(context)
        assert raised.value.code == error.code


@pytest.mark.asyncio
async def test_cache_corruption_and_ssrf_validation_fail_closed() -> None:
    artifacts, snapshot, context, url, _, _ = _fetch_case(
        source_age=timedelta(hours=1)
    )
    artifacts.byte_payloads[snapshot.storage_uri] = b"corrupted"
    operations = SearchStepOperations(
        uow_factory=None,  # type: ignore[arg-type]
        artifacts=artifacts,  # type: ignore[arg-type]
        planner=None,  # type: ignore[arg-type]
        discovery=None,  # type: ignore[arg-type]
        fetcher=StubFetcher(_successful_fetch(url, b"live must not hide corruption")),
        provider_names=("direct",),
        snapshot_reader=StubSnapshotReader(snapshot),
        url_safety_validator=RecordingURLValidator(),
        document_reuse_enabled=True,
    )
    with pytest.raises(TypedError) as raised:
        await operations.fetch(context)
    assert raised.value.code == "cache_artifact_corrupted"

    blocked = TypedError(
        ErrorCategory.PERMANENT,
        "ssrf_blocked",
        "private target",
        retryable=False,
    )
    validator = RecordingURLValidator(blocked)
    fetcher = StubFetcher(_successful_fetch(url, b"must not fetch"))
    operations.url_safety_validator = validator
    operations.fetcher = fetcher
    with pytest.raises(TypedError) as raised:
        await operations.fetch(context)
    assert raised.value.code == "ssrf_blocked"
    assert fetcher.calls == 0


@pytest.mark.asyncio
async def test_unmapped_fact_disables_cache_but_keeps_live_fetch() -> None:
    artifacts, snapshot, context, url, _, _ = _fetch_case(
        source_age=timedelta(hours=1)
    )
    artifacts.payloads[context.input_ref.uri]["hit"]["fact_ids"] = [str(uuid4())]
    reader = StubSnapshotReader(snapshot)
    fetcher = StubFetcher(_successful_fetch(url, b"live unmapped source"))
    operations = SearchStepOperations(
        uow_factory=None,  # type: ignore[arg-type]
        artifacts=artifacts,  # type: ignore[arg-type]
        planner=None,  # type: ignore[arg-type]
        discovery=None,  # type: ignore[arg-type]
        fetcher=fetcher,
        provider_names=("direct",),
        snapshot_reader=reader,
        url_safety_validator=RecordingURLValidator(),
        document_reuse_enabled=True,
    )

    await operations.fetch(context)

    assert fetcher.calls == 1
    assert reader.calls == []
    assert artifacts.last_payload["decision"] == "LIVE"  # type: ignore[index]
